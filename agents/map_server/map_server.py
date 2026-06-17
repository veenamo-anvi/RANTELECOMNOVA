"""Map server (spec §6.5 + F.4) — FastAPI :8083, Leaflet live cell map + chat panel."""
import os

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

CONTROLLER_URL = os.getenv("CONTROLLER_URL", "http://controller:8080")
ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://orchestrator:8082")

app = FastAPI(title="RAN Map Server")


class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"

VENDOR_COLOURS = {"Nokia": "#1f77b4", "Ericsson": "#2ca02c",
                  "Samsung": "#9467bd", "ZTE": "#ff7f0e"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/cells")
def api_cells():
    try:
        net = httpx.get(f"{CONTROLLER_URL}/network", timeout=15).json()
    except httpx.HTTPError as e:
        return JSONResponse({"error": str(e), "cells": []}, status_code=502)
    out = []
    for cid, c in net.get("cells", {}).items():
        k = c.get("kpi", {})
        prb = float(k.get("prb_dl_pct", 0) or 0)
        sinr = float(k.get("sinr_db", 99) or 99)
        status = "overload" if prb > 85 else ("sinr_low" if sinr < 5 else "ok")
        out.append({
            "cell_id": cid, "lat": c.get("lat"), "lon": c.get("lon"),
            "vendor": c.get("vendor"), "colour": VENDOR_COLOURS.get(c.get("vendor"), "#888"),
            "generation": c.get("generation"), "band": c.get("band"),
            "hardware_model": c.get("hardware_model"), "pci": c.get("pci"),
            "du_id": c.get("du_id"), "cu_id": c.get("cu_id"), "status": status,
            "connected_ues": k.get("connected_ues"), "prb_dl_pct": k.get("prb_dl_pct"),
            "sinr_db": k.get("sinr_db"), "rsrp_dbm": k.get("rsrp_dbm"),
            "power_w": k.get("power_w"), "dl_throughput_mbps": k.get("dl_throughput_mbps"),
        })
    return {"cells": out, "topology_version": net.get("topology_version")}


@app.post("/chat")
def chat(req: ChatRequest):
    """Proxy-stream the orchestrator's chat response to the browser."""
    def gen():
        try:
            with httpx.stream("POST", f"{ORCHESTRATOR_URL}/chat",
                              json={"message": req.message, "session_id": req.session_id},
                              timeout=120) as r:
                for chunk in r.iter_raw():
                    if chunk:
                        yield chunk
        except httpx.HTTPError as e:
            yield f"\n\n[Error] orchestrator unreachable: {e}".encode()
    return StreamingResponse(gen(), media_type="text/plain")


@app.get("/", response_class=HTMLResponse)
def index():
    return _PAGE


_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Malleswaram RAN — Live Map</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  html,body,#map{height:100%;margin:0}
  .legend{background:#fff;padding:8px 10px;font:13px sans-serif;line-height:1.5;border-radius:6px;box-shadow:0 1px 5px rgba(0,0,0,.3)}
  .dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px}
  #chat{position:absolute;top:10px;right:10px;width:330px;max-height:85vh;display:flex;flex-direction:column;
        background:#fff;border-radius:8px;box-shadow:0 2px 10px rgba(0,0,0,.35);z-index:1000;font:13px sans-serif}
  #chat h3{margin:0;padding:10px 12px;background:#1f2d3d;color:#fff;border-radius:8px 8px 0 0;font-size:14px}
  #chatlog{flex:1;overflow-y:auto;padding:10px 12px;min-height:120px}
  #chatlog .u{color:#1f2d3d;font-weight:600;margin-top:8px}
  #chatlog .a{white-space:pre-wrap;margin:2px 0 6px}
  #shortcuts{padding:6px 12px;display:flex;flex-wrap:wrap;gap:4px}
  #shortcuts button{font-size:11px;padding:3px 7px;border:1px solid #ccc;background:#f4f4f4;border-radius:10px;cursor:pointer}
  #chatform{display:flex;border-top:1px solid #eee}
  #msg{flex:1;border:0;padding:10px;outline:none;font-size:13px}
  #send{border:0;background:#1f77b4;color:#fff;padding:0 14px;cursor:pointer}
</style></head><body>
<div id="map"></div>
<div id="chat">
  <h3>Network Assistant</h3>
  <div id="shortcuts">
    <button data-m="/status">/status</button>
    <button data-m="/alerts">/alerts</button>
    <button data-m="/cells">/cells</button>
    <button data-m="/plan">/plan</button>
  </div>
  <div id="chatlog"></div>
  <form id="chatform"><input id="msg" placeholder="Ask about the network..." autocomplete="off"/>
    <button id="send" type="submit">Send</button></form>
</div>
<script>
const VENDORS={Nokia:'#1f77b4',Ericsson:'#2ca02c',Samsung:'#9467bd',ZTE:'#ff7f0e'};
const map=L.map('map').setView([13.002,77.566],14);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  {attribution:'&copy; OpenStreetMap',maxZoom:19}).addTo(map);
let layer=L.layerGroup().addTo(map);

function style(c){
  let fill=c.colour, opacity=c.generation==='5G'?0.85:0.4;
  if(c.status==='overload') fill='#d62728';
  else if(c.status==='sinr_low') fill='#ff7f0e';
  return {color:c.colour,fillColor:fill,fillOpacity:opacity,weight:2,radius:9};
}
function popup(c){
  return `<b>${c.cell_id}</b><br>${c.vendor} ${c.hardware_model||''}<br>`+
    `${c.generation} ${c.band} | PCI ${c.pci}<br>DU ${c.du_id} / CU ${c.cu_id}<br>`+
    `UEs ${c.connected_ues??'-'} | PRB ${c.prb_dl_pct??'-'}%<br>`+
    `SINR ${c.sinr_db??'-'} dB | RSRP ${c.rsrp_dbm??'-'} dBm<br>`+
    `Power ${c.power_w??'-'} W | DL ${c.dl_throughput_mbps??'-'} Mbps`;
}
async function refresh(){
  try{
    const r=await fetch('/api/cells'); const d=await r.json();
    layer.clearLayers();
    (d.cells||[]).forEach(c=>{
      if(c.lat==null||c.lon==null) return;
      L.circleMarker([c.lat,c.lon],style(c)).bindPopup(popup(c)).addTo(layer);
    });
  }catch(e){console.error(e);}
}
const legend=L.control({position:'bottomright'});
legend.onAdd=function(){
  const d=L.DomUtil.create('div','legend');
  d.innerHTML='<b>Vendor</b><br>'+Object.entries(VENDORS).map(
    ([k,v])=>`<span class="dot" style="background:${v}"></span>${k}`).join('<br>')+
    '<br><b>Status</b><br><span class="dot" style="background:#d62728"></span>Overload'+
    '<br><span class="dot" style="background:#ff7f0e"></span>SINR&lt;5dB';
  return d;
};
legend.addTo(map);
refresh(); setInterval(refresh,30000);

// --- chat panel (streams from orchestrator via map-server /chat proxy) ---
const SHORTCUTS={
  '/status':'What is the current status of all cells, DUs, and CUs? Summarise in a table.',
  '/alerts':'Show me all recent KPI alerts from the last 60 minutes.',
  '/cells':'List all cells with their current connected UEs, PRB utilisation, and DU assignment.',
  '/plan':'Generate a network plan for Malleswaram with default parameters and show me a summary.'};
const SESSION='map-'+Math.random().toString(36).slice(2,9);
const log=document.getElementById('chatlog');
function add(cls,txt){const d=document.createElement('div');d.className=cls;d.textContent=txt;log.appendChild(d);log.scrollTop=log.scrollHeight;return d;}
async function send(text){
  const shown=SHORTCUTS[text]?text:text;
  add('u','You: '+shown);
  const out=add('a','');
  try{
    const r=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:SHORTCUTS[text]||text,session_id:SESSION})});
    const reader=r.body.getReader(); const dec=new TextDecoder();
    while(true){const {done,value}=await reader.read(); if(done) break;
      out.textContent+=dec.decode(value,{stream:true}); log.scrollTop=log.scrollHeight;}
  }catch(e){out.textContent+='\\n[Error] '+e;}
}
document.getElementById('chatform').addEventListener('submit',ev=>{
  ev.preventDefault(); const i=document.getElementById('msg');
  const v=i.value.trim(); if(!v) return; i.value=''; send(v);
});
document.querySelectorAll('#shortcuts button').forEach(b=>
  b.addEventListener('click',()=>send(b.dataset.m)));
</script></body></html>"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8083)
