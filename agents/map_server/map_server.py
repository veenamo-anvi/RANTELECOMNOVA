"""Map server (spec §6.5 + F.4) — FastAPI :8083, Leaflet live cell map."""
import os

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

CONTROLLER_URL = os.getenv("CONTROLLER_URL", "http://controller:8080")
ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://orchestrator:8082")

app = FastAPI(title="RAN Map Server")

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
</style></head><body>
<div id="map"></div>
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
</script></body></html>"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8083)
