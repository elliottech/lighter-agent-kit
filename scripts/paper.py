#!/usr/bin/env python3
"""Paper trading script for Lighter Agent Kit.

Local simulation against real Lighter order book snapshots.
No credentials required - all state is local.

Shared-subset commands (order market, order ioc) use <group> <action> shape
identical to trade.py so swapping the script name swaps the engine.
Paper-only lifecycle commands stay flat (init, reset, status, etc.).
"""

import asyncio
import json
import os
import sys
import tempfile
from dataclasses import replace
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _cli import JsonArgumentParser, error, output  # noqa: E402
from _paths import paper_state_path  # noqa: E402
from _sdk import DEFAULT_HOST, ensure_lighter, get_config_value, tag_api_client  # noqa: E402
from _symbols import normalize_side, resolve_symbol  # noqa: E402

ensure_lighter()
import lighter  # noqa: E402
from lighter.paper_client import (  # noqa: E402
    AccountTier,
    InMemoryOrderBook,
    MarketConfig,
    PaperAccount,
    PaperClient,
    PaperOrderRequest,
    PaperOrderSide,
    PaperOrderType,
    PaperPosition,
    PaperTrade,
)
from lighter.paper_client.accounting import new_paper_account  # noqa: E402


# ---------------------------------------------------------------------------
# Tier mapping
# ---------------------------------------------------------------------------

TIER_MAP = {tier.name.lower(): tier for tier in AccountTier}
TIER_CHOICES = tuple(TIER_MAP.keys())


def _fee_bps(tier_enum):
    return {
        "taker_fee_bps": round(tier_enum.taker_fee * 10_000, 2),
        "maker_fee_bps": round(tier_enum.maker_fee * 10_000, 2),
    }


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

STATE_VERSION = 1


def _state_path():
    return paper_state_path()


def _ser_position(pos):
    return {
        "market_id": pos.market_id,
        "size": pos.size,
        "entry_quote": pos.entry_quote,
        "avg_entry_price": pos.avg_entry_price,
        "mark_price": pos.mark_price,
        "unrealized_pnl": pos.unrealized_pnl,
        "realized_pnl": pos.realized_pnl,
        "liquidation_price": pos.liquidation_price,
    }


def _deser_position(d):
    return PaperPosition(
        market_id=d["market_id"],
        size=d.get("size", 0),
        entry_quote=d.get("entry_quote", 0),
        avg_entry_price=d.get("avg_entry_price", 0),
        mark_price=d.get("mark_price", 0),
        unrealized_pnl=d.get("unrealized_pnl", 0),
        realized_pnl=d.get("realized_pnl", 0),
        liquidation_price=d.get("liquidation_price", 0),
    )


def _ser_trade(t):
    return {
        "market_id": t.market_id,
        "side": int(t.side),
        "size": t.size,
        "price": t.price,
        "fee": t.fee,
        "realized_pnl": t.realized_pnl,
        "is_liquidation": t.is_liquidation,
        "timestamp": t.timestamp.isoformat(),
    }


def _deser_trade(d):
    return PaperTrade(
        market_id=d["market_id"],
        side=PaperOrderSide(d["side"]),
        size=d["size"],
        price=d["price"],
        fee=d["fee"],
        realized_pnl=d["realized_pnl"],
        is_liquidation=d["is_liquidation"],
        timestamp=datetime.fromisoformat(d["timestamp"]),
    )


def _ser_account(acct):
    return {
        "initial_collateral": acct.initial_collateral,
        "collateral": acct.collateral,
        "positions": {
            str(k): _ser_position(v) for k, v in acct.positions.items()
        },
        "trades": [_ser_trade(t) for t in acct.trades],
    }


def _deser_account(d):
    return PaperAccount(
        initial_collateral=d["initial_collateral"],
        collateral=d["collateral"],
        positions={
            int(k): _deser_position(v)
            for k, v in d.get("positions", {}).items()
        },
        trades=[_deser_trade(t) for t in d.get("trades", [])],
    )


def _ser_config(cfg):
    return {
        "market_id": cfg.market_id,
        "symbol": cfg.symbol,
        "size_decimals": cfg.size_decimals,
        "price_decimals": cfg.price_decimals,
        "default_initial_margin_fraction": cfg.default_initial_margin_fraction,
        "min_initial_margin_fraction": cfg.min_initial_margin_fraction,
        "maintenance_margin_fraction": cfg.maintenance_margin_fraction,
        "closeout_margin_fraction": cfg.closeout_margin_fraction,
        "taker_fee": cfg.taker_fee,
        "maker_fee": cfg.maker_fee,
        "min_base_amount": cfg.min_base_amount,
        "min_quote_amount": cfg.min_quote_amount,
        "last_trade_price": cfg.last_trade_price,
    }


def _deser_config(d):
    return MarketConfig(**d)


def _save_state(tier_name, account, market_configs):
    state = {
        "version": STATE_VERSION,
        "tier": tier_name,
        "account": _ser_account(account),
        "market_configs": {
            str(k): _ser_config(v) for k, v in market_configs.items()
        },
    }
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f"{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
        tmp = f.name
    os.replace(tmp, path)


def _state_corrupt_error(path, detail):
    error(
        f"paper state file at {path} is corrupted ({detail}); "
        f"run `paper.py reset` or `rm {path}` to start fresh"
    )


class StateValidationError(Exception):
    def __init__(self, detail, *, version_mismatch=False):
        super().__init__(detail)
        self.detail = detail
        self.version_mismatch = version_mismatch


def _validate_state_data(data):
    if not isinstance(data, dict):
        raise StateValidationError(
            f"expected top-level JSON object, got {type(data).__name__}"
        )
    if data.get("version") != STATE_VERSION:
        raise StateValidationError(
            f"got {data.get('version')}, expected {STATE_VERSION}",
            version_mismatch=True,
        )
    tier_name = data.get("tier")
    if not isinstance(tier_name, str) or tier_name not in TIER_MAP:
        expected = ", ".join(TIER_MAP.keys())
        raise StateValidationError(
            f"invalid tier {tier_name!r}; expected one of: {expected}",
        )
    account = data.get("account")
    if not isinstance(account, dict):
        raise StateValidationError("missing or invalid 'account' object")
    market_configs = data.get("market_configs", {})
    if not isinstance(market_configs, dict):
        raise StateValidationError(
            "invalid 'market_configs'; expected object keyed by market_id"
        )
    return data


def _load_state():
    path = _state_path()
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        error(f"paper state file at {path} exists but cannot be read: {e}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        _state_corrupt_error(path, f"JSON parse error: {e.msg} at line {e.lineno}")
    try:
        return _validate_state_data(data)
    except StateValidationError as e:
        if e.version_mismatch:
            error(
                f"paper state version mismatch at {path} "
                f"({e.detail}); run `paper.py reset` to reinitialize"
            )
        _state_corrupt_error(path, e.detail)


def _try_load_state():
    path = _state_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    try:
        return _validate_state_data(data)
    except StateValidationError:
        return None


def _require_state():
    state = _load_state()
    if state is None:
        error("no paper account; run `paper.py init` first")
    return state


def _unpack_state(state):
    tier_name = state["tier"]
    tier_enum = TIER_MAP[tier_name]
    account = _deser_account(state["account"])
    configs = {
        int(k): _deser_config(v)
        for k, v in state.get("market_configs", {}).items()
    }
    return tier_name, tier_enum, account, configs


# ---------------------------------------------------------------------------
# Symbol resolution
# ---------------------------------------------------------------------------

def _resolve_symbol_cached(symbol, market_configs):
    market_id = _cached_market_id(symbol, market_configs)
    if market_id is not None:
        return market_id
    error(
        f"unknown symbol '{symbol}'; place an order or use "
        f"`paper.py refresh --symbol {symbol}` to discover it"
    )


def _cached_market_id(symbol, market_configs):
    sym = symbol.upper()
    for mid, cfg in market_configs.items():
        if cfg.symbol.upper() == sym:
            return mid
    return None




def _symbol_for_market(market_id, market_configs):
    cfg = market_configs.get(market_id)
    return cfg.symbol if cfg else str(market_id)


# ---------------------------------------------------------------------------
# PaperClient lifecycle helpers
# ---------------------------------------------------------------------------

def _hydrate_paper_client(api_client, tier_enum, account, configs):
    """Rebuild a PaperClient from persisted state."""
    paper = PaperClient(
        api_client,
        account.initial_collateral,
        account_tier=tier_enum,
    )
    paper.account = account
    paper.market_configs = dict(configs)
    for mid, pos in account.positions.items():
        if pos.size == 0:
            continue
        paper.order_books.setdefault(mid, InMemoryOrderBook())
        cfg = paper.market_configs.get(mid)
        if cfg is not None and pos.mark_price > 0:
            paper.market_configs[mid] = replace(
                cfg, last_trade_price=pos.mark_price,
            )
    return paper


async def _refresh_position_markets(paper):
    """Refresh mark prices for all markets with open positions, in parallel.

    Returns (refreshed_market_ids, failures) where failures is a dict mapping
    symbol -> error string for markets whose refresh raised. Partial success
    is kept: successful markets get fresh marks, failed ones retain cached.
    """
    market_ids = [
        mid for mid, pos in paper.account.positions.items()
        if pos.size != 0
    ]
    if not market_ids:
        return [], {}
    results = await asyncio.gather(
        *(paper.track_market_snapshot(mid) for mid in market_ids),
        return_exceptions=True,
    )
    refreshed = []
    failures = {}
    for mid, res in zip(market_ids, results):
        if isinstance(res, BaseException):
            symbol = _symbol_for_market(mid, paper.market_configs)
            failures[symbol] = f"{type(res).__name__}: {res}"
        else:
            refreshed.append(mid)
    return refreshed, failures


async def _run_read(operation, *, refresh=True):
    state = _require_state()
    tier_name, tier_enum, account, configs = _unpack_state(state)
    host = get_config_value("LIGHTER_HOST", DEFAULT_HOST)
    failures = {}
    async with lighter.ApiClient(
        configuration=lighter.Configuration(host=host),
    ) as api_client:
        tag_api_client(api_client)
        paper = _hydrate_paper_client(api_client, tier_enum, account, configs)
        if refresh:
            _, failures = await _refresh_position_markets(paper)
        result = await operation(paper)
        _save_state(tier_name, paper.account, paper.market_configs)
        return tier_name, tier_enum, paper, result, failures


def _attach_warnings(payload, failures):
    if failures:
        payload.setdefault("warnings", {})["refresh_failed"] = failures
    return payload


async def _run_with_paper_market(symbol, operation=None):
    state = _require_state()
    tier_name, tier_enum, account, configs = _unpack_state(state)
    host = get_config_value("LIGHTER_HOST", DEFAULT_HOST)

    async with lighter.ApiClient(
        configuration=lighter.Configuration(host=host),
    ) as api_client:
        tag_api_client(api_client)
        try:
            market_id, market_type, _ = await resolve_symbol(
                symbol,
                host,
                api_client,
            )
        except ValueError as e:
            error(str(e))
        if market_type != "perp":
            error(
                "paper trading supports perp markets only; "
                "use a perp symbol like ETH, not ETH/USDC"
            )
        paper = _hydrate_paper_client(api_client, tier_enum, account, configs)
        await paper.track_market_snapshot(market_id)

        result = None
        if operation is not None:
            result = await operation(paper, market_id)

        _save_state(tier_name, paper.account, paper.market_configs)
        return paper, market_id, result


# ---------------------------------------------------------------------------
# Paper-only flat commands
# ---------------------------------------------------------------------------

async def cmd_init(args):
    state = _load_state()
    if state is not None:
        error(
            "paper account already exists; use `paper.py reset` to "
            "reinitialize"
        )
    collateral = args.collateral
    tier_name = args.tier
    tier_enum = TIER_MAP[tier_name]
    account = new_paper_account(collateral)
    _save_state(tier_name, account, {})
    output({
        "status": "ok",
        "collateral": collateral,
        "tier": tier_name,
        **_fee_bps(tier_enum),
        "state_path": str(_state_path()),
    })


async def cmd_reset(args):
    state = _try_load_state()
    if state is None:
        collateral = args.collateral if args.collateral is not None else 10_000
        tier_name = args.tier if args.tier is not None else "premium"
    else:
        prev = state["account"]
        collateral = (
            args.collateral
            if args.collateral is not None
            else prev["initial_collateral"]
        )
        tier_name = (
            args.tier if args.tier is not None else state["tier"]
        )
    tier_enum = TIER_MAP[tier_name]
    account = new_paper_account(collateral)
    _save_state(tier_name, account, {})
    output({
        "status": "ok",
        "collateral": collateral,
        "tier": tier_name,
        **_fee_bps(tier_enum),
        "state_path": str(_state_path()),
    })


async def cmd_set_tier(args):
    state = _require_state()
    _, _, account, configs = _unpack_state(state)
    new_tier_name = args.tier
    new_tier_enum = TIER_MAP[new_tier_name]
    updated_configs = {
        mid: replace(
            cfg,
            taker_fee=new_tier_enum.taker_fee,
            maker_fee=new_tier_enum.maker_fee,
        )
        for mid, cfg in configs.items()
    }
    _save_state(new_tier_name, account, updated_configs)
    output({
        "status": "ok",
        "tier": new_tier_name,
        **_fee_bps(new_tier_enum),
    })


async def cmd_status(args):
    async def op(paper):
        return paper.get_account()

    tier_name, tier_enum, _, account, failures = await _run_read(
        op, refresh=not getattr(args, "no_refresh", False),
    )
    total_unrealized = sum(
        pos.unrealized_pnl for pos in account.positions.values()
    )
    output(_attach_warnings({
        "status": "ok",
        "collateral": account.collateral,
        "initial_collateral": account.initial_collateral,
        "tier": tier_name,
        **_fee_bps(tier_enum),
        "unrealized_pnl": total_unrealized,
        "total_pnl": account.collateral - account.initial_collateral
        + total_unrealized,
        "positions_count": len(account.positions),
        "trades_count": len(account.trades),
        "state_path": str(_state_path()),
    }, failures))


async def cmd_positions(args):
    async def op(paper):
        return paper.get_account()

    _, _, paper, account, failures = await _run_read(
        op, refresh=not getattr(args, "no_refresh", False),
    )
    configs = paper.market_configs
    positions = []
    for mid, pos in account.positions.items():
        positions.append({
            "symbol": _symbol_for_market(mid, configs),
            "market_id": mid,
            "side": "long" if pos.size > 0 else "short",
            "size": abs(pos.size),
            "avg_entry_price": pos.avg_entry_price,
            "mark_price": pos.mark_price,
            "unrealized_pnl": pos.unrealized_pnl,
            "realized_pnl": pos.realized_pnl,
            "liquidation_price": pos.liquidation_price,
        })
    if args.symbol is not None:
        sym = args.symbol.upper()
        positions = [p for p in positions if p["symbol"].upper() == sym]
    output(_attach_warnings({"positions": positions}, failures))


async def cmd_trades(args):
    state = _require_state()
    _, _, account, configs = _unpack_state(state)
    trades = list(account.trades)
    if args.symbol is not None:
        mid = _resolve_symbol_cached(args.symbol, configs)
        trades = [t for t in trades if t.market_id == mid]
    trades = list(reversed(trades))[: args.limit]
    output({
        "trades": [
            {
                "symbol": _symbol_for_market(t.market_id, configs),
                "market_id": t.market_id,
                "side": "buy" if t.side == PaperOrderSide.BUY else "sell",
                "size": t.size,
                "price": t.price,
                "fee": t.fee,
                "realized_pnl": t.realized_pnl,
                "is_liquidation": t.is_liquidation,
                "timestamp": t.timestamp.isoformat(),
            }
            for t in trades
        ],
    })


async def cmd_health(args):
    async def op(paper):
        return paper.get_health(), paper.get_account()

    tier_name, tier_enum, _, (health, account), failures = await _run_read(
        op, refresh=not getattr(args, "no_refresh", False),
    )
    output(_attach_warnings({
        "status": health.status.name.lower(),
        "total_account_value": health.total_account_value,
        "initial_margin_requirement": health.initial_margin_requirement,
        "maintenance_margin_requirement": health.maintenance_margin_requirement,
        "margin_usage": round(health.margin_usage, 2),
        "leverage": round(health.leverage, 4),
        "collateral": account.collateral,
        "tier": tier_name,
        **_fee_bps(tier_enum),
    }, failures))


async def cmd_liquidation_price(args):
    async def op(paper):
        market_id = _resolve_symbol_cached(args.symbol, paper.market_configs)
        pos = paper.account.positions.get(market_id)
        if pos is None or pos.size == 0:
            return market_id, None
        liq_price = paper.get_liquidation_price(market_id)
        return market_id, (pos, liq_price)

    _, _, _, (market_id, payload), failures = await _run_read(
        op, refresh=not getattr(args, "no_refresh", False),
    )
    if payload is None:
        output(_attach_warnings({
            "symbol": args.symbol.upper(),
            "market_id": market_id,
            "liquidation_price": 0,
            "note": "no open position",
        }, failures))
        return
    pos, liq_price = payload
    output(_attach_warnings({
        "symbol": args.symbol.upper(),
        "market_id": market_id,
        "liquidation_price": liq_price,
        "mark_price": pos.mark_price,
        "position_side": "long" if pos.size > 0 else "short",
        "position_size": abs(pos.size),
    }, failures))


async def cmd_refresh(args):
    paper, market_id, _ = await _run_with_paper_market(args.symbol)
    book = paper.order_books.get(market_id)
    mid_price = book.mid_price if book else None
    output({
        "status": "ok",
        "symbol": _symbol_for_market(market_id, paper.market_configs),
        "market_id": market_id,
        "mid_price": mid_price,
        "best_bid": book.best_bid.price if book and book.best_bid else None,
        "best_ask": book.best_ask.price if book and book.best_ask else None,
    })


# ---------------------------------------------------------------------------
# Shared-subset commands — <group> <action>, mirrors trade.py
# ---------------------------------------------------------------------------

SIDE_CHOICES = ["buy", "sell", "long", "short"]


def _liquidated_requested_market(paper, market_id, trades_before):
    new_trades = paper.get_trades()[trades_before:]
    return any(
        trade.is_liquidation and trade.market_id == market_id
        for trade in new_trades
    )


async def cmd_order_market(args):
    if args.amount <= 0:
        error("--amount must be positive")

    normalized = normalize_side(args.side, "perp")

    async def _place(paper, market_id):
        side = PaperOrderSide.BUY if normalized in ("long", "buy") else PaperOrderSide.SELL
        request = PaperOrderRequest(
            market_id=market_id,
            side=side,
            base_amount=args.amount,
            order_type=PaperOrderType.MARKET,
        )
        trades_before = len(paper.get_trades())
        result = await paper.create_paper_order(request)
        liquidated = _liquidated_requested_market(
            paper, market_id, trades_before,
        )
        return result, liquidated

    paper, market_id, (result, liquidated) = await _run_with_paper_market(args.symbol, _place)
    symbol = _symbol_for_market(market_id, paper.market_configs)
    output({
        "status": "ok",
        "symbol": symbol,
        "market_id": market_id,
        "side": normalized,
        "order_type": "market",
        "filled_size": result.filled_size,
        "avg_price": result.avg_price,
        "total_fee": result.total_fee,
        "quote_amount": result.quote_amount,
        "unfilled": result.unfilled,
        "liquidated": liquidated,
        "fills_count": len(result.fills),
    })


async def cmd_order_ioc(args):
    if args.amount <= 0:
        error("--amount must be positive")
    if args.price <= 0:
        error("--price must be positive")

    normalized = normalize_side(args.side, "perp")

    async def _place(paper, market_id):
        side = PaperOrderSide.BUY if normalized in ("long", "buy") else PaperOrderSide.SELL
        request = PaperOrderRequest(
            market_id=market_id,
            side=side,
            base_amount=args.amount,
            price=args.price,
            order_type=PaperOrderType.IOC,
        )
        trades_before = len(paper.get_trades())
        result = await paper.create_paper_order(request)
        liquidated = _liquidated_requested_market(
            paper, market_id, trades_before,
        )
        return result, liquidated

    paper, market_id, (result, liquidated) = await _run_with_paper_market(args.symbol, _place)
    symbol = _symbol_for_market(market_id, paper.market_configs)
    output({
        "status": "ok",
        "symbol": symbol,
        "market_id": market_id,
        "side": normalized,
        "order_type": "ioc",
        "limit_price": args.price,
        "filled_size": result.filled_size,
        "avg_price": result.avg_price,
        "total_fee": result.total_fee,
        "quote_amount": result.quote_amount,
        "unfilled": result.unfilled,
        "liquidated": liquidated,
        "fills_count": len(result.fills),
    })


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

_PAPER_EPILOG = (
    "Response shape: references/schemas-paper.md. "
    "Paper vs live caveats: SKILL.md § Paper Trading."
)


def build_parser():
    parser = JsonArgumentParser(
        prog="paper.py",
        description=(
            "Paper trading on Lighter (local simulation against real "
            "order book snapshots)"
        ),
        epilog=_PAPER_EPILOG,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # -- Paper-only flat commands --

    p = sub.add_parser("init", help="Create a new paper trading account", epilog=_PAPER_EPILOG)
    p.add_argument("--collateral", type=float, default=10_000, help="Starting USDC (default: 10000)")
    p.add_argument("--tier", default="premium", choices=TIER_CHOICES, help="Fee tier (default: premium)")

    p = sub.add_parser("reset", help="Reset paper account", epilog=_PAPER_EPILOG)
    p.add_argument("--collateral", type=float, default=None, help="New starting collateral")
    p.add_argument("--tier", default=None, choices=TIER_CHOICES, help="New fee tier")

    p = sub.add_parser("set_tier", help="Change fee tier on existing account", epilog=_PAPER_EPILOG)
    p.add_argument("--tier", required=True, choices=TIER_CHOICES)

    p = sub.add_parser("status", help="Paper account summary", epilog=_PAPER_EPILOG)
    p.add_argument("--no-refresh", action="store_true",
                   help="Skip auto-refresh of mark prices (faster; shows cached values)")

    p = sub.add_parser("positions", help="Open paper positions", epilog=_PAPER_EPILOG)
    p.add_argument("--symbol", help="Filter to one symbol (e.g. ETH)")
    p.add_argument("--no-refresh", action="store_true",
                   help="Skip auto-refresh of mark prices (faster; shows cached values)")

    p = sub.add_parser("trades", help="Paper trade history (most recent first)", epilog=_PAPER_EPILOG)
    p.add_argument("--symbol", help="Filter to one symbol")
    p.add_argument("--limit", type=int, default=50, help="Max trades (default: 50)")

    p = sub.add_parser("health", help="Paper account health and margin status", epilog=_PAPER_EPILOG)
    p.add_argument("--no-refresh", action="store_true",
                   help="Skip auto-refresh of mark prices (faster; shows cached values)")

    p = sub.add_parser("liquidation_price", help="Estimated liquidation price for a position", epilog=_PAPER_EPILOG)
    p.add_argument("symbol", help="Symbol (e.g. ETH)")
    p.add_argument("--no-refresh", action="store_true",
                   help="Skip auto-refresh of mark prices (faster; shows cached values)")

    p = sub.add_parser("refresh", help="Force-refresh order book snapshot (diagnostic)", epilog=_PAPER_EPILOG)
    p.add_argument("symbol", help="Symbol (e.g. ETH)")

    # -- Shared-subset commands: order <action> --

    order = sub.add_parser("order", help="Order operations (shared with trade.py)")
    order_sub = order.add_subparsers(dest="action", required=True)

    p = order_sub.add_parser("market", help="Paper market order (taker-only)", epilog=_PAPER_EPILOG)
    p.add_argument("symbol", help="Symbol (e.g. BTC)")
    p.add_argument("--side", required=True, choices=SIDE_CHOICES,
                   help="Order side: buy|sell|long|short")
    p.add_argument("--amount", type=float, required=True, help="Base amount (human units)")

    p = order_sub.add_parser("ioc", help="Paper IOC order with limit price (taker-only)", epilog=_PAPER_EPILOG)
    p.add_argument("symbol", help="Symbol (e.g. BTC)")
    p.add_argument("--side", required=True, choices=SIDE_CHOICES,
                   help="Order side: buy|sell|long|short")
    p.add_argument("--amount", type=float, required=True, help="Base amount (human units)")
    p.add_argument("--price", type=float, required=True, help="Limit price (human units)")

    return parser


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

FLAT_COMMANDS = {
    "init": cmd_init,
    "reset": cmd_reset,
    "set_tier": cmd_set_tier,
    "status": cmd_status,
    "positions": cmd_positions,
    "trades": cmd_trades,
    "health": cmd_health,
    "liquidation_price": cmd_liquidation_price,
    "refresh": cmd_refresh,
}

GROUPED_COMMANDS = {
    ("order", "market"): cmd_order_market,
    ("order", "ioc"): cmd_order_ioc,
}


async def run(args):
    command = args.command
    action = getattr(args, "action", None)

    if command in FLAT_COMMANDS:
        handler = FLAT_COMMANDS[command]
    elif action and (command, action) in GROUPED_COMMANDS:
        handler = GROUPED_COMMANDS[(command, action)]
    else:
        label = f"{command} {action}" if action else command
        error(f"unknown command: {label}")

    try:
        await handler(args)
    except lighter.ApiException as e:
        detail = e.reason
        if e.body:
            try:
                detail = json.loads(e.body).get("message", e.reason)
            except json.JSONDecodeError:
                pass
        error(f"API error {e.status}: {detail}")
    except SystemExit:
        raise
    except Exception as e:
        error(str(e))


def main():
    args = build_parser().parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
