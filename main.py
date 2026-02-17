import os
import requests
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from typing import List, Optional, Literal, Dict, Any

app = FastAPI(title="AlphaGuard US API", version="1.0.0")

ALPACA_KEY = os.getenv("ALPACA_KEY_ID", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_DATA_BASE = "https://data.alpaca.markets"


# -------------------------------
# Helpers
# -------------------------------

def alpaca_headers():
    if not ALPACA_KEY or not ALPACA_SECRET:
        raise HTTPException(500, "Missing ALPACA_KEY_ID / ALPACA_SECRET_KEY")
    return {
        "APCA-API-KEY-ID": ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    }


def iso_utc(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_extract_bars(payload: Dict[str, Any], symbol: str) -> List[Dict[str, Any]]:
    """
    兼容 Alpaca 多种 bars 结构
    """
    sym = symbol.upper()
    bars_obj = payload.get("bars")

    # 1️⃣ bars 是 dict
    if isinstance(bars_obj, dict):
        v = bars_obj.get(sym)
        if isinstance(v, list):
            return [b for b in v if isinstance(b, dict)]

    # 2️⃣ bars 是 list
    if isinstance(bars_obj, list):
        if not bars_obj:
            return []
        # 多标的结构（每根 bar 带 S 字段）
        if isinstance(bars_obj[0], dict) and "S" in bars_obj[0]:
            return [b for b in bars_obj if isinstance(b, dict) and b.get("S") == sym]
        # 单标的结构
        return [b for b in bars_obj if isinstance(b, dict)]

    return []


# -------------------------------
# Models
# -------------------------------

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


# -------------------------------
# Endpoints
# -------------------------------

@app.get("/us/bars")
def get_us_bars(
    symbol: str = Query(...),
    tf: str = Query("15Min"),
    limit: int = Query(500, ge=50, le=5000),
    session: str = Query("regular"),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
):
    if session != "regular":
        raise HTTPException(400, "MVP supports session=regular only")

    # 默认拉最近7天
    now = datetime.now(timezone.utc)
    if start is None:
        start = iso_utc(now - timedelta(days=7))
    if end is None:
        end = iso_utc(now)

    url = f"{ALPACA_DATA_BASE}/v2/stocks/bars"
    params = {
        "symbols": symbol.upper(),
        "timeframe": tf,
        "limit": limit,
        "start": start,
        "end": end,
        "feed": "iex"
    }

    try:
        r = requests.get(url, headers=alpaca_headers(), params=params, timeout=20)
    except Exception:
        # 网络异常时不500，直接返回空
        return {"market": "US", "symbol": symbol.upper(), "bars": []}

    if r.status_code != 200:
        return {"market": "US", "symbol": symbol.upper(), "bars": []}

    try:
        data = r.json()
    except Exception:
        return {"market": "US", "symbol": symbol.upper(), "bars": []}

    bars_list = safe_extract_bars(data, symbol)

    out = []
    for b in bars_list:
        if not all(k in b for k in ["t", "o", "h", "l", "c", "v"]):
            continue
        out.append({
            "timestamp": b["t"],
            "open": b["o"],
            "high": b["h"],
            "low": b["l"],
            "close": b["c"],
            "volume": b["v"],
        })

    return {
        "market": "US",
        "symbol": symbol.upper(),
        "timeframe": tf,
        "session": session,
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

    return GuardrailsOut(validation_level="L2", analysis_blocked=False)


@app.post("/ta/us", response_model=TAOut)
def ta_us(payload: TAIn):
    bars = payload.bars
    if len(bars) < 60:
        return TAOut(trend_state="unknown", trend_conflict=False, invalidation="insufficient data")

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
