#!/usr/bin/env python3
"""Read-only query script for Lighter Agent Kit.

Commands follow <group> <action> pattern to mirror the Lighter UI.
"""

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _cli import JsonArgumentParser, error, output  # noqa: E402
from _paths import credentials_path  # noqa: E402
from _sdk import (  # noqa: E402
    DEFAULT_HOST,
    ensure_lighter,
    get_config_value,
    resolve_with_source,
    tag_api_client,
)
from _symbols import resolve_symbol  # noqa: E402

# Lazy: SDK is only imported on demand so `auth status` (and other no-SDK
# diagnostics) stay fast on cold sessions where vendoring would otherwise
# burn 15–40s.
lighter = None


def _load_sdk():
    global lighter
    if lighter is None:
        ensure_lighter()
        import lighter as _lighter
        lighter = _lighter
    return lighter


def _schema_epilog(section):
    return f"Response shape: see references/schemas-read.md § {section}"


def _position_size(p):
    """Best-effort float conversion of a position row's size field."""
    try:
        return float(p.get("position", 0))
    except (TypeError, ValueError):
        return 0.0


def build_parser():
    parser = JsonArgumentParser(
        prog="query.py",
        description="Query Lighter API — <group> <action> commands",
        epilog="Response shapes: references/schemas-read.md. Workflow: SKILL.md.",
    )
    sub = parser.add_subparsers(dest="group", required=True)

    # -------------------------------------------------------------------------
    # system <action>
    # -------------------------------------------------------------------------
    system = sub.add_parser("system", help="System-level queries")
    system_sub = system.add_subparsers(dest="action", required=True)

    system_sub.add_parser(
        "status",
        help="System health/status",
        epilog=_schema_epilog("status"),
    )

    # -------------------------------------------------------------------------
    # market <action>
    # -------------------------------------------------------------------------
    market = sub.add_parser("market", help="Market data queries")
    market_sub = market.add_subparsers(dest="action", required=True)

    p = market_sub.add_parser(
        "list",
        help="Compact symbol→market_index catalog",
        epilog=_schema_epilog("markets"),
    )
    p.add_argument(
        "--market_type",
        choices=["perp", "spot"],
        help="Filter by market type",
    )
    p.add_argument(
        "--search",
        help="Case-insensitive substring match against symbol",
    )

    p = market_sub.add_parser(
        "stats",
        help="Market overview (prices, volumes, funding)",
        epilog=_schema_epilog("exchange_stats"),
    )
    p.add_argument(
        "--symbol",
        help="Filter to one symbol (e.g. ETH)",
    )

    p = market_sub.add_parser(
        "info",
        help="Full market metadata (fees, decimals, min sizes)",
        epilog=_schema_epilog("order_books"),
    )
    p.add_argument(
        "--market_type",
        choices=["perp", "spot"],
        help="Filter by market type",
    )
    p.add_argument(
        "--symbol",
        help="Filter to one symbol (e.g. ETH)",
    )

    p = market_sub.add_parser(
        "book",
        help="Order book depth for a market",
        epilog=_schema_epilog("order_book_details"),
    )
    p.add_argument(
        "symbol",
        help="Perp (e.g. BTC) or spot pair (e.g. ETH/USDC), or numeric market_index",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max bid/ask levels per side (default: 20)",
    )

    p = market_sub.add_parser(
        "trades",
        help="Recent trades for a market",
        epilog=_schema_epilog("recent_trades"),
    )
    p.add_argument(
        "symbol",
        help="Perp (e.g. BTC) or spot pair (e.g. ETH/USDC), or numeric market_index",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max trades (default: 20, max: 100)",
    )

    p = market_sub.add_parser(
        "candles",
        help="OHLCV candles for a market",
        epilog=_schema_epilog("candles"),
    )
    p.add_argument(
        "symbol",
        help="Perp (e.g. BTC) or spot pair (e.g. ETH/USDC), or numeric market_index",
    )
    p.add_argument(
        "--resolution",
        default="1h",
        help="Candle resolution: 1m,5m,15m,30m,1h,4h,1d (default: 1h)",
    )
    p.add_argument(
        "--count_back",
        type=int,
        default=24,
        help="Number of candles (default: 24)",
    )
    p.add_argument("--start_timestamp", type=int, help="Start unix seconds")
    p.add_argument("--end_timestamp", type=int, help="End unix seconds")

    p = market_sub.add_parser(
        "funding",
        help="Funding rates across venues",
        epilog=_schema_epilog("funding_rates"),
    )
    p.add_argument("--symbol", help="Filter to one symbol (e.g. ETH)")
    p.add_argument("--market_index", type=int, help="Filter to one market_index")
    p.add_argument("--exchange", help="Filter to one exchange (e.g. binance)")

    # -------------------------------------------------------------------------
    # account <action>
    # -------------------------------------------------------------------------
    account = sub.add_parser("account", help="Account queries")
    account_sub = account.add_subparsers(dest="action", required=True)

    p = account_sub.add_parser(
        "info",
        help="Account info (balances, positions, assets)",
        epilog=_schema_epilog("account"),
    )
    p.add_argument(
        "--account_index",
        type=int,
        help="Account index (default: LIGHTER_ACCOUNT_INDEX env var)",
    )
    p.add_argument(
        "--by",
        choices=["index", "l1_address"],
        help="Lookup type (default: index)",
    )
    p.add_argument(
        "--value",
        help="Lookup value (L1 address when --by l1_address)",
    )
    p.add_argument(
        "--include_zero_positions",
        action="store_true",
        help=(
            "Include positions with size 0 in the response. By default they "
            "are filtered out. Lighter keeps one row per market the account "
            "has ever touched (for cumulative realized_pnl / funding history), "
            "so pass this flag when analysing lifetime PnL or funding paid."
        ),
    )

    account_sub.add_parser(
        "limits",
        help="Trading limits and tier info (self-only — uses LIGHTER_ACCOUNT_INDEX)",
        epilog=_schema_epilog("account_limits"),
    )

    p = account_sub.add_parser(
        "apikeys",
        help="List API keys for an account",
        epilog=_schema_epilog("apikeys"),
    )
    p.add_argument(
        "--account_index",
        type=int,
        help="Account index (default: LIGHTER_ACCOUNT_INDEX env var)",
    )
    p.add_argument(
        "--api_key_index",
        type=int,
        default=255,
        help="Specific key index, or 255 for all (default: 255)",
    )

    # -------------------------------------------------------------------------
    # portfolio <action>
    # -------------------------------------------------------------------------
    portfolio = sub.add_parser("portfolio", help="Portfolio queries")
    portfolio_sub = portfolio.add_subparsers(dest="action", required=True)

    p = portfolio_sub.add_parser(
        "performance",
        help="PnL chart over time (self-only — uses LIGHTER_ACCOUNT_INDEX)",
        epilog=_schema_epilog("pnl"),
    )
    p.add_argument("--resolution", default="1h")
    p.add_argument("--count_back", type=int, default=24)
    p.add_argument("--start_timestamp", type=int)
    p.add_argument("--end_timestamp", type=int)
    p.add_argument("--ignore_transfers", action="store_true")

    # -------------------------------------------------------------------------
    # orders <action>
    # -------------------------------------------------------------------------
    orders = sub.add_parser("orders", help="Order queries")
    orders_sub = orders.add_subparsers(dest="action", required=True)

    p = orders_sub.add_parser(
        "open",
        help="Open orders on a market (self-only — uses LIGHTER_ACCOUNT_INDEX)",
        epilog=_schema_epilog("account_active_orders / account_inactive_orders"),
    )
    p.add_argument(
        "--symbol",
        help="Perp (e.g. BTC) or spot pair (e.g. ETH/USDC). Alternative to --market_index.",
    )
    p.add_argument(
        "--market_index",
        type=int,
        help="Market index. Pass either --symbol or --market_index.",
    )

    p = orders_sub.add_parser(
        "history",
        help="Order history, filled/cancelled (self-only — uses LIGHTER_ACCOUNT_INDEX)",
        epilog=_schema_epilog("account_active_orders / account_inactive_orders"),
    )
    p.add_argument(
        "--symbol",
        help="Filter to one market by symbol (perp ticker or spot pair).",
    )
    p.add_argument("--market_index", type=int, help="Filter to one market by index.")
    p.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max orders (default: 20, max: 100)",
    )

    # -------------------------------------------------------------------------
    # auth <action>
    # -------------------------------------------------------------------------
    auth = sub.add_parser(
        "auth",
        help="Credential introspection (local only, no network, no SDK load)",
    )
    auth_sub = auth.add_subparsers(dest="action", required=True)
    auth_sub.add_parser(
        "status",
        help="Report which credentials are resolved and from which source",
        epilog=_schema_epilog("auth_status"),
    )

    return parser


RESOLUTION_SECONDS = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}


def resolve_time_range(args):
    """Compute (start, end) seconds from args, defaulting to count_back * resolution ago."""
    end_ts = getattr(args, "end_timestamp", None)
    start_ts = getattr(args, "start_timestamp", None)
    resolution = getattr(args, "resolution", "1h")
    count_back = getattr(args, "count_back", 24)

    end = end_ts if end_ts is not None else int(time.time())
    if start_ts is not None:
        return start_ts, end
    if resolution not in RESOLUTION_SECONDS:
        valid = ", ".join(RESOLUTION_SECONDS.keys())
        error(f"unsupported resolution '{resolution}', use one of: {valid}")
    seconds = RESOLUTION_SECONDS[resolution]
    return end - seconds * max(count_back, 1), end


def get_account_index(args) -> int:
    """Get account_index from args (public reads) or env var."""
    if getattr(args, "account_index", None) is not None:
        return args.account_index
    env_val = get_config_value("LIGHTER_ACCOUNT_INDEX")
    if env_val is not None:
        try:
            return int(env_val)
        except ValueError:
            error("LIGHTER_ACCOUNT_INDEX must be an integer")
    error(
        "missing --account_index; set it explicitly or via LIGHTER_ACCOUNT_INDEX env var"
    )


def require_self_account_index() -> int:
    """Return LIGHTER_ACCOUNT_INDEX or error.

    For authenticated reads (account limits, portfolio performance, orders
    open/history).
    """
    env_val = get_config_value("LIGHTER_ACCOUNT_INDEX")
    if env_val is None:
        error(
            "this command requires LIGHTER_ACCOUNT_INDEX; set it as an env "
            "var or in your lighter-agent-kit credentials file"
        )
    try:
        return int(env_val)
    except ValueError:
        error("LIGHTER_ACCOUNT_INDEX must be an integer")


async def get_auth_token(host):
    """Generate a read-only auth token from configured credentials."""
    api_private_key = get_config_value("LIGHTER_API_PRIVATE_KEY")
    account_index_str = get_config_value("LIGHTER_ACCOUNT_INDEX")
    if not api_private_key or not account_index_str:
        error(
            "this command requires LIGHTER_API_PRIVATE_KEY and "
            "LIGHTER_ACCOUNT_INDEX; set them as env vars or in your "
            "lighter-agent-kit credentials file"
        )
    try:
        account_index = int(account_index_str)
    except ValueError:
        error("LIGHTER_ACCOUNT_INDEX must be an integer")

    api_key_index_str = get_config_value("LIGHTER_API_KEY_INDEX")
    if api_key_index_str is None:
        error(
            "this command requires LIGHTER_API_KEY_INDEX; set it as an env var "
            "or in your lighter-agent-kit credentials file"
        )
    try:
        api_key_index = int(api_key_index_str)
    except ValueError:
        error("LIGHTER_API_KEY_INDEX must be an integer")

    try:
        signer = lighter.SignerClient(
            url=host,
            account_index=account_index,
            api_private_keys={api_key_index: api_private_key.expose()},
        )
        tag_api_client(signer.api_client)
    except Exception as e:
        error(f"failed to initialize signer for auth token: {e}")
    try:
        token, err = signer.create_auth_token_with_expiry(api_key_index=api_key_index)
        if err is not None:
            error(f"failed to generate auth token: {err}")
        return token
    finally:
        await signer.api_client.close()


def cmd_auth_status():
    """Synchronous, local-only credential introspection.

    Reports the resolution outcome of every credential the skill consults,
    along with where each value came from. Never returns secret values:
    presence-only for `LIGHTER_API_PRIVATE_KEY`, raw int for the indices,
    raw string for `LIGHTER_HOST`.

    Use this as a precheck before invoking authenticated reads or writes —
    it's a single command that subsumes manual env-var probing and avoids
    silently missing the credentials-file source.
    """
    required = ("LIGHTER_API_PRIVATE_KEY", "LIGHTER_ACCOUNT_INDEX", "LIGHTER_API_KEY_INDEX")
    sources = {}
    missing = []

    for name in required:
        value, source = resolve_with_source(name)
        sources[name] = source
        if value is None:
            missing.append(name)

    host_value, host_source = resolve_with_source("LIGHTER_HOST")
    if host_value is None:
        host_value = DEFAULT_HOST
        host_source = "default"
    sources["LIGHTER_HOST"] = host_source

    def _coerce_int(name):
        raw, _ = resolve_with_source(name)
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    account_index = _coerce_int("LIGHTER_ACCOUNT_INDEX")
    api_key_index = _coerce_int("LIGHTER_API_KEY_INDEX")

    creds_path = credentials_path()
    creds_present = creds_path.is_file()
    mode_secure = None
    if creds_present and os.name != "nt":
        try:
            mode_secure = (creds_path.stat().st_mode & 0o077) == 0
        except OSError:
            mode_secure = None

    output(
        {
            "status": "ok",
            "auth_capable": not missing,
            "host": host_value,
            "account_index": account_index,
            "api_key_index": api_key_index,
            "sources": sources,
            "credentials_file": {
                "path": str(creds_path),
                "present": creds_present,
                "mode_secure": mode_secure,
            },
            "missing": missing,
        }
    )


async def run(args):
    _load_sdk()
    host = get_config_value("LIGHTER_HOST", DEFAULT_HOST)
    config = lighter.Configuration(host=host)

    try:
        async with lighter.ApiClient(configuration=config) as client:
            tag_api_client(client)
            group = args.group
            action = args.action

            # -----------------------------------------------------------------
            # system
            # -----------------------------------------------------------------
            if group == "system":
                if action == "status":
                    api = lighter.RootApi(client)
                    output((await api.status()).to_dict())

            # -----------------------------------------------------------------
            # market
            # -----------------------------------------------------------------
            elif group == "market":
                if action == "list":
                    api = lighter.OrderApi(client)
                    books = (await api.order_books()).to_dict().get("order_books", [])
                    if args.market_type is not None:
                        books = [
                            ob
                            for ob in books
                            if ob.get("market_type") == args.market_type
                        ]
                    if args.search is not None:
                        needle = args.search.upper()
                        books = [
                            ob
                            for ob in books
                            if needle in ob.get("symbol", "").upper()
                        ]
                    markets_list = [
                        {
                            "symbol": ob.get("symbol"),
                            "market_index": ob.get("market_id"),
                            "market_type": ob.get("market_type"),
                        }
                        for ob in books
                    ]
                    result = {"code": 200, "markets": markets_list}
                    if (
                        args.search is not None
                        and args.market_type is None
                        and len({m["market_type"] for m in markets_list}) > 1
                    ):
                        result["filter_hint"] = (
                            "result spans multiple market_types; "
                            "retry with --market_type perp|spot to narrow"
                        )
                    output(result)

                elif action == "stats":
                    api = lighter.OrderApi(client)
                    result = (await api.exchange_stats()).to_dict()
                    if args.symbol is not None:
                        sym = args.symbol.upper()
                        result["order_book_stats"] = [
                            s
                            for s in result.get("order_book_stats", [])
                            if s.get("symbol", "").upper() == sym
                        ]
                    output(result)

                elif action == "info":
                    api = lighter.OrderApi(client)
                    result = (await api.order_books()).to_dict()
                    obs = result.get("order_books", [])
                    if args.market_type is not None:
                        obs = [
                            ob
                            for ob in obs
                            if ob.get("market_type") == args.market_type
                        ]
                    if args.symbol is not None:
                        sym = args.symbol.upper()
                        obs = [ob for ob in obs if ob.get("symbol", "").upper() == sym]
                    result["order_books"] = obs
                    output(result)

                elif action == "book":
                    api = lighter.OrderApi(client)
                    try:
                        market_index, _, _ = await resolve_symbol(
                            args.symbol,
                            host,
                            client,
                        )
                    except ValueError as e:
                        error(str(e))
                    books = await api.order_books()
                    valid_ids = {ob.market_id for ob in books.order_books}
                    if market_index not in valid_ids:
                        error(
                            f"unknown market_index {market_index}; "
                            f"use `query.py market list` to list valid markets"
                        )
                    result = await api.order_book_orders(
                        market_id=market_index,
                        limit=args.limit,
                    )
                    output(result.to_dict())

                elif action == "trades":
                    api = lighter.OrderApi(client)
                    try:
                        market_index, _, _ = await resolve_symbol(
                            args.symbol,
                            host,
                            client,
                        )
                    except ValueError as e:
                        error(str(e))
                    result = await api.recent_trades(
                        market_id=market_index,
                        limit=args.limit,
                    )
                    output(result.to_dict())

                elif action == "candles":
                    api = lighter.CandlestickApi(client)
                    try:
                        market_index, _, _ = await resolve_symbol(
                            args.symbol,
                            host,
                            client,
                        )
                    except ValueError as e:
                        error(str(e))
                    start, end = resolve_time_range(args)
                    result = await api.candles(
                        market_id=market_index,
                        resolution=args.resolution,
                        start_timestamp=start,
                        end_timestamp=end,
                        count_back=args.count_back,
                    )
                    output(result.to_dict())

                elif action == "funding":
                    api = lighter.FundingApi(client)
                    result = (await api.funding_rates()).to_dict()
                    rates = result.get("funding_rates", [])
                    if args.symbol is not None:
                        sym = args.symbol.upper()
                        rates = [
                            r for r in rates if r.get("symbol", "").upper() == sym
                        ]
                    if args.market_index is not None:
                        rates = [
                            r for r in rates if r.get("market_id") == args.market_index
                        ]
                    if args.exchange is not None:
                        exc = args.exchange.lower()
                        rates = [
                            r for r in rates if r.get("exchange", "").lower() == exc
                        ]
                    result["funding_rates"] = rates
                    output(result)

            # -----------------------------------------------------------------
            # account
            # -----------------------------------------------------------------
            elif group == "account":
                if action == "info":
                    api = lighter.AccountApi(client)
                    # Support --by l1_address --value 0x... for address lookup
                    if args.by == "l1_address" and args.value:
                        result = await api.account(by="l1_address", value=args.value)
                    else:
                        account_index = get_account_index(args)
                        result = await api.account(by="index", value=str(account_index))
                    payload = result.to_dict()
                    if not args.include_zero_positions:
                        for acct in payload.get("accounts", []):
                            acct["positions"] = [
                                p for p in acct.get("positions", [])
                                if _position_size(p) > 0
                            ]
                    output(payload)

                elif action == "limits":
                    api = lighter.AccountApi(client)
                    auth = await get_auth_token(host)
                    account_index = require_self_account_index()
                    result = await api.account_limits(
                        account_index=account_index,
                        auth=auth,
                    )
                    output(result.to_dict())

                elif action == "apikeys":
                    api = lighter.AccountApi(client)
                    account_index = get_account_index(args)
                    result = await api.apikeys(
                        account_index=account_index,
                        api_key_index=args.api_key_index,
                    )
                    output(result.to_dict())

            # -----------------------------------------------------------------
            # portfolio
            # -----------------------------------------------------------------
            elif group == "portfolio":
                if action == "performance":
                    api = lighter.AccountApi(client)
                    auth = await get_auth_token(host)
                    account_index = require_self_account_index()
                    start, end = resolve_time_range(args)
                    result = await api.pnl(
                        by="index",
                        value=str(account_index),
                        resolution=args.resolution,
                        start_timestamp=start,
                        end_timestamp=end,
                        count_back=args.count_back,
                        ignore_transfers=args.ignore_transfers or None,
                        auth=auth,
                    )
                    output(result.to_dict())

            # -----------------------------------------------------------------
            # orders
            # -----------------------------------------------------------------
            elif group == "orders":
                if action == "open":
                    if args.market_index is None and args.symbol is None:
                        error("orders open requires --symbol or --market_index")
                    market_id = args.market_index
                    if market_id is None:
                        try:
                            market_id, _, _ = await resolve_symbol(
                                args.symbol, host, client,
                            )
                        except ValueError as e:
                            error(str(e))
                    api = lighter.OrderApi(client)
                    auth = await get_auth_token(host)
                    account_index = require_self_account_index()
                    result = await api.account_active_orders(
                        account_index=account_index,
                        market_id=market_id,
                        auth=auth,
                    )
                    output(result.to_dict())

                elif action == "history":
                    market_id = args.market_index
                    if market_id is None and args.symbol is not None:
                        try:
                            market_id, _, _ = await resolve_symbol(
                                args.symbol, host, client,
                            )
                        except ValueError as e:
                            error(str(e))
                    api = lighter.OrderApi(client)
                    auth = await get_auth_token(host)
                    account_index = require_self_account_index()
                    kwargs = {
                        "account_index": account_index,
                        "limit": args.limit,
                        "auth": auth,
                    }
                    if market_id is not None:
                        kwargs["market_id"] = market_id
                    result = await api.account_inactive_orders(**kwargs)
                    output(result.to_dict())

    except lighter.ApiException as e:
        detail = e.reason
        if e.body:
            try:
                detail = json.loads(e.body).get("message", e.reason)
            except json.JSONDecodeError:
                pass
        error(f"API error {e.status}: {detail}")
    except Exception as e:
        error(str(e))


def main():
    args = build_parser().parse_args()
    if args.group == "auth" and args.action == "status":
        cmd_auth_status()
        return
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
