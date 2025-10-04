import os, time, statistics, collections, requests

HEALTH_URL = os.getenv("SERVICE_HEALTH_URL", "http://13.203.205.92/health")
PROBE_URL  = os.getenv("SERVICE_PROBE_URL",  "http://13.203.205.92/")  # any cheap GET
TIMEOUT_S  = float(os.getenv("PROBE_TIMEOUT", "2.0"))
WINDOW     = int(os.getenv("PROBE_WINDOW", "20"))  # rolling window size

# rolling window of (latency_ms, ok_bool) samples
samples = collections.deque(maxlen=WINDOW)

def sample_once():
    """Ping /health for liveness and PROBE_URL for latency/error; store in window."""
    # 1) liveness via /health
    up = False
    try:
        r = requests.get(HEALTH_URL, timeout=TIMEOUT_S)
        up = (r.status_code == 200)
    except Exception:
        up = False

    # 2) synthetic probe for latency/error
    t0 = time.perf_counter()
    ok = False
    try:
        rp = requests.get(PROBE_URL, timeout=TIMEOUT_S)
        ok = (200 <= rp.status_code < 400)
    except Exception:
        ok = False
    lat_ms = int((time.perf_counter() - t0) * 1000)

    samples.append((lat_ms, ok))
    return up, lat_ms, ok

def aggregate():
    """Compute error_rate and p95 latency over the rolling window."""
    if not samples:
        return {"error_rate": 0.0, "p95_ms": 0}
    lats = [s[0] for s in samples]
    oks  = [s[1] for s in samples]
    error_rate = 1.0 - (sum(oks) / len(oks))
    if len(lats) >= 20:
        # p95 via quantiles (needs >= 20 for stable p95); else use max as a proxy
        p95 = int(statistics.quantiles(lats, n=100)[94])
    else:
        p95 = max(lats)
    return {"error_rate": round(error_rate, 3), "p95_ms": p95}
