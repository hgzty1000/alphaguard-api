import os
import requests
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from typing import List, Optional, Literal

app = FastAPI(title="AlphaGuard US API", version="0.1.0")

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

@app.get("/us/bars")
def get_us_bars(
    symbol: str = Query(...),
    tf: str = Query("15Min"),
    limit: int = Query(500, ge=50, le=5000),
    session: str = Query("regular"),
):
    if session != "regular":
        raise HTTPException(400, "MVP supports session=regular only")

    url = f"{ALPACA_DATA_BASE}/v2/stocks/bars"
    params = {"symbols": symbol.upper(), "timeframe": tf, "limit": limit}
    r = requests.get(url, headers=alpaca_headers(), params=params, timeout=20)
    if r.status_code != 200:
        raise HTTPException(r.status_code, f"Alpaca error: {r.text}")

    data = r.json()
    bars = data.get("bars", [])
    out = [{
        "timestamp": b["t"],
        "open": b["o"],
        "high": b["h"],
        "low": b["l"],
        "close": b["c"],
        "volume": b["v"],
    } for b in bars]
    return {"market": "US", "symbol": symbol.upper(), "timeframe": tf, "session": session, "bars": out}

@app.post("/guardrails", response_model=GuardrailsOut)
def guardrails(payload: GuardrailsIn):
    bars = payload.bars
    if len(bars) < 50:
        return GuardrailsOut(validation_level="L1", analysis_blocked=False, warnings=["bars_too_few"])

    ts = [b.timestamp for b in bars]
    if any(ts[i] <= ts[i-1] for i in range(1, len(ts))):
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

    # 超简版趋势：看最近收盘相对区间位置
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
