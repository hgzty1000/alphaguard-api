import os
import requests
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from typing import List, Optional, Literal, Any, Dict

app = FastAPI(title="AlphaGuard US API", version="0.3.0")

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
def _iso_utc(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_bar_list_from_columnar(d: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    有些返回可能是列式：
    {"t":[...], "o":[...], "h":[...], "l":[...], "c":[...], "v":[...]}
    """
    keys = ["t", "o", "h", "l", "c", "v"]
    if not all(isinstance(d.get(k), list) for k in keys):
        return []
    t, o, h, l, c, v = (d["t"], d["o"], d["h"], d["l"], d["c"], d["v"])
    n = min(len(t), len(o), len(h), len(l), len(c), len(v))
    return [{"t": t[i], "o": o[i], "h": h[i], "l": l[i], "c": c[i], "v": v[i]} for i in range(n)]


def _normalize_alpaca_bars(payload: Dict[str, Any], symbol: str) -> List[Dict[str, Any]]:
    """
    兼容 Alpaca bars 多种返回形态，最终统一为 list[dict]，dict 至少含 t,o,h,l,c,v
    """
    sym = symbol.upper()
    bars_obj = payload.get("bars", None)

    # 1) bars 是 dict：{"AAPL": [ {...}, {...} ]} 或 {"AAPL": {...列式...}}
    if isinstance(bars_obj, dict):
        v = bars_obj.get(sym) or bars_obj.get(symbol) or bars_obj.get(sym.replace(".", ""))
        if isinstance(v, list):
            # list 中必须是 dict，否则返回空
            return [x for x in v if isinstance(x, dict)]
        if isinstance(v, dict):
            # a) 可能内层还有 bars list
            if isinstance(v.get("bars"), list):
                return [x for x in v["bars"] if isinstance(x, dict)]
            # b) 可能是列式结构
            col = _to_bar_list_from_columnar(v)
            if col:
                return col
            # c) 兜底：如果 v 本身就是单根 bar dict（极少见）
            if all(k in v for k in ["t", "o", "h", "l", "c", "v"]):
                return [v]
        # 如果 bars_obj 本身就是 “symbol->something” 但不是 list/dict，返回空
        return []

    # 2) bars 是 list：可能每个元素含 "S" 标记所属 symbol（多标的）
    if isinstance(bars_obj, list):
        if not bars_obj:
            return []
        if isinstance(bars_obj[0], dict):
            # a) 多标的 list：每根 bar 里带 "S"
            if "S" in bars_obj[0]:
                return [b for b in bars_obj if isinstance(b, dict) and b.get("S") == sym]
            # b) 单标的 list：直接就是 bar dict
            return [b for b in bars_obj if isinstance(b, dict)]
        # 如果 list 元素不是 dict（比如字符串），直接判为空
        return []

    return []


def _get_bar_field(b: Dict[str, Any], key: str) -> Any:
    """
    兼容大小写 / 可能的替代键
    """
    if key in b:
        return b[key]
    # 一些实现会出现大写（不常见，但兜底）
    alt = key.upper()
    if alt in b:
        return b[alt]
    return None


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

    # 默认拉最近 7 天，避免盘前/当天无 bars
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
        "feed": "iex",
    }

    r = requests.get(url, headers=alpaca_headers(), params=params, timeout=20)
    if r.status_code != 200:
        raise HTTPException(r.status_code, f"Alpaca error: {r.text}")

    data = r.json()
    bars_list = _normalize_alpaca_bars(data, symbol)

    # 这里再做一层防御：如果仍不是 list[dict]，就直接返回空而不是 500
    out = []
    for b in bars_list:
        if not isinstance(b, dict):
            continue

        t = _get_bar_field(b, "t")
        o = _get_bar_field(b, "o")
        h = _get_bar_field(b, "h")
        l = _get_bar_field(b, "l")
        c = _get_bar_field(b, "c")
        v = _get_bar_field(b, "v")

        if t is None or o is None or h is None or l is None or c is None or v is None:
            # 跳过不完整 bar，避免炸
            continue

        out.append({
            "timestamp": t,
            "open": o,
            "high": h,
            "low": l,
            "close": c,
            "volume": v,
        })

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
