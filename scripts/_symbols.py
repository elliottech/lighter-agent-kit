"""Symbol resolution for lighter-agent-kit scripts.

Resolves human-readable symbols to market_index values.

Convention:
- Perp markets use bare tickers:      BTC, ETH, SOL, LIT
- Spot markets use quote-qualified pairs: ETH/USDC, LIT/USDC, LINK/USDC

The `/` in a symbol string is the single discriminator between market types —
no overlap, no ambiguity, no inference from --side or --market_type.

- All environments: fetches from /api/v1/orderBooks and caches on disk
- Cache TTL is 5 minutes and is shared across script invocations
"""

from __future__ import annotations

import json
import os
import tempfile
import time

from _paths import symbol_cache_path

_CACHE_TTL_SECONDS = 300
_LIVE_CACHE = {}  # host -> {"host", "fetched_at", "expires_at", "symbols"}


def _normalize_host(host: str) -> str:
    return host.strip().rstrip("/")


def _empty_symbols() -> dict:
    return {"perp": {}, "spot": {}}


def _is_valid_symbols(symbols) -> bool:
    if not isinstance(symbols, dict):
        return False
    for market_type in ("perp", "spot"):
        bucket = symbols.get(market_type)
        if not isinstance(bucket, dict):
            return False
        if not all(isinstance(sym, str) and isinstance(mid, int) for sym, mid in bucket.items()):
            return False
    return True


def _is_fresh(entry: dict | None, now: int | None = None) -> bool:
    if not isinstance(entry, dict):
        return False
    expires_at = entry.get("expires_at")
    if not isinstance(expires_at, int):
        return False
    if now is None:
        now = int(time.time())
    return now < expires_at


def _build_cache_entry(host: str, symbols: dict, now: int | None = None) -> dict:
    if now is None:
        now = int(time.time())
    return {
        "host": host,
        "fetched_at": now,
        "expires_at": now + _CACHE_TTL_SECONDS,
        "symbols": symbols,
    }


def _read_disk_cache(host: str) -> dict | None:
    path = symbol_cache_path(host)
    if not path.is_file():
        return None

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None
    if payload.get("host") != host:
        return None
    if not _is_valid_symbols(payload.get("symbols")):
        return None
    return payload


def _write_disk_cache(host: str, symbols: dict) -> dict:
    path = symbol_cache_path(host)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = _build_cache_entry(host, symbols)

    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f"{path.name}.",
        suffix=".tmp",
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(json.dumps(entry, separators=(",", ":")))
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    return entry


async def _fetch_symbols(api_client) -> dict:
    """Fetch symbols from /api/v1/orderBooks."""
    import lighter

    order_api = lighter.OrderApi(api_client)
    result = await order_api.order_books()
    symbols = _empty_symbols()
    for ob in result.order_books:
        mt = ob.market_type
        if mt in symbols:
            symbols[mt][ob.symbol] = ob.market_id
    return symbols


async def _get_live_symbols(host: str, api_client) -> dict:
    """Return a live symbol map for the host with a shared 5-minute TTL.

    The cache is persisted on disk so separate `query.py` / `trade.py` /
    `paper.py` invocations can share it.
    """
    host = _normalize_host(host)
    cached = _LIVE_CACHE.get(host)
    if _is_fresh(cached):
        return cached["symbols"]

    cached = _read_disk_cache(host)
    if _is_fresh(cached):
        _LIVE_CACHE[host] = cached
        return cached["symbols"]

    symbols = await _fetch_symbols(api_client)
    entry = _write_disk_cache(host, symbols)
    _LIVE_CACHE[host] = entry
    return entry["symbols"]


async def _refresh_live_symbols(host: str, api_client) -> dict:
    host = _normalize_host(host)
    symbols = await _fetch_symbols(api_client)
    entry = _write_disk_cache(host, symbols)
    _LIVE_CACHE[host] = entry
    return entry["symbols"]


def _find_market_by_index(symbols: dict, market_index: int):
    for market_type in ("perp", "spot"):
        for symbol, mid in symbols.get(market_type, {}).items():
            if mid == market_index:
                return (market_type, symbol)
    return None


def _parse_symbol_or_index(value: str):
    """Parse a value that could be a symbol or numeric market_index.

    Returns (symbol, None) or (None, market_index).
    """
    try:
        return (None, int(value))
    except ValueError:
        return (value.upper(), None)


async def resolve_symbol(
    symbol_or_index: str,
    host: str,
    api_client,
):
    """Resolve a symbol or market_index to (market_index, market_type, symbol).

    Symbol convention:
    - Bare ticker (no /)  -> perp     (e.g. BTC)
    - Contains /          -> spot     (e.g. ETH/USDC)

    Numeric market_index is accepted as an escape hatch.

    Symbol metadata is fetched from the live order-books API and shared across
    script invocations via a disk-backed 5-minute TTL cache.
    """
    symbol, market_index = _parse_symbol_or_index(symbol_or_index)
    host = _normalize_host(host)

    if market_index is not None:
        primary = await _get_live_symbols(host, api_client)
        found = _find_market_by_index(primary, market_index)
        if found is None:
            primary = await _refresh_live_symbols(host, api_client)
        found = _find_market_by_index(primary, market_index)
        if found is not None:
            market_type, symbol = found
            return (market_index, market_type, symbol)
        return (market_index, "perp", str(market_index))

    market_type = "spot" if "/" in symbol else "perp"
    primary = await _get_live_symbols(host, api_client)
    if symbol in primary.get(market_type, {}):
        return (primary[market_type][symbol], market_type, symbol)

    # A still-fresh cache can be incomplete if a previous fetch wrote a
    # truncated snapshot. Retry once with a fresh live fetch before failing.
    primary = await _refresh_live_symbols(host, api_client)
    if symbol in primary.get(market_type, {}):
        return (primary[market_type][symbol], market_type, symbol)

    raise ValueError(
        f"unknown symbol '{symbol}'; use `query.py market list --search {symbol}` "
        f"to discover available markets"
    )


def normalize_side(side: str, market_type: str) -> str:
    """Normalize side to canonical form for the market type.

    perp: long/short
    spot: buy/sell

    Accepts: buy, sell, long, short (case-insensitive).
    """
    side = side.lower()
    if side not in ("buy", "sell", "long", "short"):
        raise ValueError(f"invalid side '{side}'; use buy|sell|long|short")

    if market_type == "perp":
        return "long" if side in ("buy", "long") else "short"
    return "buy" if side in ("buy", "long") else "sell"


def side_to_is_ask(side: str) -> bool:
    """Convert normalized side to is_ask boolean for SDK."""
    return side in ("sell", "short")
