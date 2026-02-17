import os
import requests
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from typing import List, Optional, Literal, Any, Dict

app = FastAPI(title="AlphaGuard US API", version="0.2.0")

ALPACA_KEY = os.getenv("ALPACA_KEY_ID", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_DATA_BASE = "https://data.alpaca.markets"

def alpaca_headers():
    if not ALPACA_KEY or not ALPACA_SECRET:
        raise HTTPException(500, "Missing ALPACA_KEY_ID / ALPACA_SECRET_KEY env vars")
    return {
        "APCA-API-KEY-ID": ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }

# ---------------- Models ----------------
class OHLCVBar(BaseModel):
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float

class GuardrailsIn(BaseModel):
    market: Literal["US"] = "US"
    timeframe: str
    bars: List[OHLCVBar]

class GuardrailsOut(BaseModel):
    validation_level: Literal["L0", "L1", "L2"]
    analysis_blocked: bool
    block_reason: Optional[str] = None
    warnings: List[str] = []

class TAIn(BaseModel):
    market: Literal["US"] = "US"
    timeframe: str
    bars: List[OHLCVBar]

class TAOut(BaseModel):
    trend_state: Literal["up", "down", "range", "unknown"]
    trend_conflict: bool
    S1: Optional[float] = None
    R1: Optional[float] = None
    invalidation: str

# ---------------- Helpers ----------------
def _normalize_alpaca_bars(payload: Dict[str, Any], symbol: str) -> List[Dict[str, Any]]:
    """
    Alpaca /v2/stocks/bars 可能返回：
    - {"bars": {"AAPL": [ {...}, {...} ]}}
    - 或 {"bars": [ {...}, {...} ]} （较少见）
    这里统一成 list[dict]
    """
    bars_obj = payload.get("bars", {})

    if isinstance(bars_obj, dict):
        return bars_obj.get(symbol.upper(), []) or []
    if isinstance(bars_obj, list):
        return bars_obj
    return []

def _iso_utc(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

# ---------------- Endpoints ----------------
@app.get("/us/bars")
def get_us_bars(
    symbol: str = Query(..., description="US ticker, e.g. AAPL"),
    tf: str = Query("15Min", description="Alpaca timeframe: 15Min / 1Day etc."),
    limit: int = Query(500, ge=50, le=5000),
    session: str = Query("regular", description="MVP: regular only"),
    start: Optional[str] = Query(None, description="ISO8601 UTC, e.g. 2026-02-01T00:00:00Z"),
    end: Optional[str] = Query(None, description="ISO8601 UTC, e.g. 2026-02-10T00:00:00Z"),
):
    if session != "regular":
        raise HTTPException(400, "MVP supports session=regular only")

    # ✅ 默认拉最近 7 天，避免盘前/当天无 bars 返回空
    now = datetime.now(timezone.utc)
    if start is None:
        start = _iso_utc(now - timedelta(days=7))
    if end is None:
        end = _iso_utc(now)

    url = f"{ALPACA_DATA_BASE}/v2/stocks/bars"
    params = {
        "symbols": symbol.upper(),
        "timeframe": tf,
        "limit": limit,
        "start": start,
        "end": end,
        # 免费/默认通常用 iex；如你有 SIP 订阅可改成 "sip"
        "feed": "iex",
    }

    r = requests.get(url, headers=alpaca_headers(), params=params, timeout=20)
    if r.status_code != 200:
        raise HTTPException(r.status_code, f"Alpaca error: {r.text}")

    data = r.json()
    bars_list = _normalize_alpaca_bars(data, symbol)

    out = [{
        "timestamp": b["t"],
        "open": b["o"],
        "high": b["h"],
        "low": b["l"],
        "close": b["c"],
        "volume": b["v"],
    } for b in bars_list]

    return {
        "market": "US",
        "symbol": symbol.upper(),
        "timeframe": tf,
        "session": session,
        "start": start,
        "end": end,
        "bars": out
    }

@app.post("/guardrails", response_model=GuardrailsOut)
def guardrails(payload: GuardrailsIn):
    bars = payload.bars
    if len(bars) < 50:
        return GuardrailsOut(validation_level="L1", analysis_blocked=False, warnings=["bars_too_few"])

    ts = [b.timestamp for b in bars]
    if any(ts[i] <= ts[i - 1] for i in range(1, len(ts))):
        return GuardrailsOut(validation_level="L0", analysis_blocked=True, block_reason="time_invalid")

    bad = 0
    for b in bars:
        if b.high < max(b.open, b.close) or b.low > min(b.open, b.close) or b.low > b.high:
            bad += 1
    ratio = bad / len(bars)

    if ratio > 0.01:
        return GuardrailsOut(validation_level="L0", analysis_blocked=True, block_reason="ohlc_invalid")
    elif ratio > 0:
        return GuardrailsOut(validation_level="L1", analysis_blocked=False, warnings=["ohlc_minor_issues"])

    return GuardrailsOut(validation_level="L2", analysis_blocked=False, warnings=[])

@app.post("/ta/us", response_model=TAOut)
def ta_us(payload: TAIn):
    bars = payload.bars
    if len(bars) < 60:
        return TAOut(trend_state="unknown", trend_conflict=False, invalidation="unknown (insufficient bars)")

    window = bars[-60:]
    s1 = min(b.low for b in window)
    r1 = max(b.high for b in window)

    last = bars[-1].close
    mid = (s1 + r1) / 2
    if last > mid:
        trend = "up"
    elif last < mid:
        trend = "down"
    else:
        trend = "range"

    return TAOut(
        trend_state=trend,
        trend_conflict=False,
        S1=round(s1, 2),
        R1=round(r1, 2),
        invalidation=f"close < {s1:.2f}"
    )

@app.get("/market_context/us")
def market_context_us():
    return {"market_state": "neutral"}
