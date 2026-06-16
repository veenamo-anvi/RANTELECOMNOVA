"""Slice allocator (spec Appendix E.2)."""

MIN_PRB = {"eMBB": 0.10, "URLLC": 0.05, "mMTC": 0.02}
LATENCY_TARGET_MS = {"eMBB": 30, "URLLC": 1, "mMTC": 100}
TDD_BANDS = {"n78", "n41", "n257", "n258", "n260", "n261"}


def allocate(traffic_profile, total_max_ues, e2e_ms=10.0):
    """traffic_profile: {eMBB,URLLC,mMTC} mix (need not sum to 1).
    Returns {slice: {prb_fraction, max_ues, latency_target_ms}}, warnings.
    """
    mix = {s: max(0.0, float(traffic_profile.get(s, 0.0))) for s in MIN_PRB}
    total = sum(mix.values()) or 1.0
    norm = {s: mix[s] / total for s in mix}

    # enforce MIN_PRB floor, then re-normalise to 1.0
    floored = {s: max(norm[s], MIN_PRB[s]) for s in norm}
    fsum = sum(floored.values())
    prb = {s: floored[s] / fsum for s in floored}

    warnings = []
    if e2e_ms <= 1 and prb["URLLC"] < 0.15:
        warnings.append(
            f"e2e_ms={e2e_ms} <= 1 but URLLC PRB fraction {prb['URLLC']:.3f} < 0.15"
        )

    slices = {
        s: {
            "prb_fraction": round(prb[s], 4),
            "max_ues": int(total_max_ues * prb[s]),
            "latency_target_ms": LATENCY_TARGET_MS[s],
        }
        for s in prb
    }
    return slices, warnings


def timing_sync_strategy(bands, fronthaul_us):
    has_tdd = any(b in TDD_BANDS for b in bands)
    if fronthaul_us <= 50 or has_tdd:
        return "IEEE-1588-PTP-Class-C"
    if fronthaul_us <= 200:
        return "IEEE-1588-PTP-Class-B"
    return "SyncE"
