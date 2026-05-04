#!/usr/bin/env python3
"""Write operations script for Lighter Agent Kit.

Commands follow <group> <action> pattern to mirror the Lighter UI.
"""

import asyncio
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _cli import JsonArgumentParser, error, output  # noqa: E402
from _sdk import DEFAULT_HOST, ensure_lighter, get_config_value, tag_api_client  # noqa: E402
from _symbols import normalize_side, resolve_symbol, side_to_is_ask  # noqa: E402

ensure_lighter()
import lighter  # noqa: E402


_SDK_ERR_RE = re.compile(r"message='([^']*)'")


def clean_sdk_error(msg):
    """Extract the human-readable message from a Lighter SDK error string."""
    if not msg:
        return msg
    m = _SDK_ERR_RE.search(msg)
    if m:
        return m.group(1)
    return msg


ASSETS = {
    "usdc": "ASSET_ID_USDC",
    "eth": "ASSET_ID_ETH",
    "lit": "ASSET_ID_LIT",
    "link": "ASSET_ID_LINK",
    "uni": "ASSET_ID_UNI",
    "aave": "ASSET_ID_AAVE",
    "sky": "ASSET_ID_SKY",
    "ldo": "ASSET_ID_LDO",
}

SIDE_CHOICES = ["buy", "sell", "long", "short"]

_WRITE_EPILOG = (
    "Response shape: references/schemas-write.md (common envelope, "
    "precision echoback, error-code table)."
)


def build_parser():
    parser = JsonArgumentParser(
        prog="trade.py",
        description="Execute trades on Lighter — <group> <action> commands",
        epilog="Response shapes: references/schemas-write.md. Workflow: SKILL.md.",
    )
    sub = parser.add_subparsers(dest="group", required=True)

    # -------------------------------------------------------------------------
    # order <action>
    # -------------------------------------------------------------------------
    order = sub.add_parser("order", help="Order operations")
    order_sub = order.add_subparsers(dest="action", required=True)

    p = order_sub.add_parser(
        "market",
        help="Place a market order",
        epilog=_WRITE_EPILOG,
    )
    p.add_argument(
        "symbol",
        help="Perp (e.g. BTC) or spot pair (e.g. ETH/USDC), or numeric market_index",
    )
    p.add_argument(
        "--side",
        required=True,
        choices=SIDE_CHOICES,
        help="Order side: buy|sell (spot), long|short (perp) — both accepted on either",
    )
    p.add_argument(
        "--amount",
        type=float,
        required=True,
        help="Base amount (human units)",
    )
    p.add_argument(
        "--slippage",
        type=float,
        default=0.01,
        help="Max slippage (default: 0.01 = 1%%)",
    )
    p.add_argument(
        "--reduce_only",
        action="store_true",
        help="Only reduce existing position (perp only; no-op on spot)",
    )
    p.add_argument("--client_order_index", type=int)

    p = order_sub.add_parser(
        "limit",
        help="Place a limit order",
        epilog=_WRITE_EPILOG,
    )
    p.add_argument(
        "symbol",
        help="Perp (e.g. BTC) or spot pair (e.g. ETH/USDC), or numeric market_index",
    )
    p.add_argument(
        "--side",
        required=True,
        choices=SIDE_CHOICES,
        help="Order side: buy|sell (spot), long|short (perp) — both accepted on either",
    )
    p.add_argument(
        "--amount",
        type=float,
        required=True,
        help="Base amount (human units)",
    )
    p.add_argument(
        "--price",
        type=float,
        required=True,
        help="Limit price (human units)",
    )
    p.add_argument(
        "--reduce_only",
        action="store_true",
        help="Only reduce existing position (perp only; no-op on spot)",
    )
    p.add_argument(
        "--post_only",
        action="store_true",
        help="Reject if order would cross the book (maker-only)",
    )
    p.add_argument("--client_order_index", type=int)

    p = order_sub.add_parser(
        "modify",
        help="Modify an open order",
        epilog=_WRITE_EPILOG,
    )
    p.add_argument(
        "symbol",
        help="Perp (e.g. BTC) or spot pair (e.g. ETH/USDC), or numeric market_index",
    )
    p.add_argument(
        "--order_index",
        type=int,
        required=True,
        help="Client order index from when the order was created",
    )
    p.add_argument("--price", type=float, required=True)
    p.add_argument("--amount", type=float, required=True)

    p = order_sub.add_parser(
        "cancel",
        help="Cancel a single open order",
        epilog=_WRITE_EPILOG,
    )
    p.add_argument(
        "symbol",
        help="Perp (e.g. BTC) or spot pair (e.g. ETH/USDC), or numeric market_index",
    )
    p.add_argument(
        "--order_index",
        type=int,
        required=True,
        help="Client order index from when the order was created",
    )

    order_sub.add_parser(
        "cancel_all",
        help="Cancel all open orders across all markets",
        epilog=_WRITE_EPILOG,
    )

    p = order_sub.add_parser(
        "close_all",
        help="[HIGH-RISK] Flatten all open positions with reduce-only market orders",
        description=(
            "[HIGH-RISK] Flattens every open position with a reduce-only "
            "market order per market, realizing PnL on all of them at once. "
            "Agents MUST run --preview first, show the plan to the user, and "
            "get explicit confirmation before invoking the real form. Do not "
            "infer intent from vague prompts like 'clean up' or 'reset'."
        ),
        epilog=_WRITE_EPILOG,
    )
    p.add_argument(
        "--slippage",
        type=float,
        default=0.01,
        help="Max slippage per closing order (default: 0.01 = 1%%)",
    )
    p.add_argument(
        "--with_cancel_all",
        action="store_true",
        help=(
            "Cancel every open order before closing. Prevents a lingering "
            "non-reduce-only order from reopening a position mid-close. "
            "Note this also kills TP/SL bracket orders."
        ),
    )
    p.add_argument(
        "--preview",
        action="store_true",
        help="List positions that would be closed without broadcasting anything",
    )

    # -------------------------------------------------------------------------
    # position <action>
    # -------------------------------------------------------------------------
    position = sub.add_parser("position", help="Position operations")
    position_sub = position.add_subparsers(dest="action", required=True)

    p = position_sub.add_parser(
        "leverage",
        help="Set leverage for a market",
        epilog=_WRITE_EPILOG,
    )
    p.add_argument(
        "symbol",
        help="Perp (e.g. BTC) or spot pair (e.g. ETH/USDC), or numeric market_index",
    )
    p.add_argument(
        "--leverage",
        type=int,
        required=True,
        help="Leverage multiplier (e.g. 10)",
    )
    p.add_argument(
        "--margin_mode",
        default="cross",
        choices=["cross", "isolated"],
    )

    p = position_sub.add_parser(
        "margin",
        help="Add or remove isolated margin collateral",
        epilog=_WRITE_EPILOG,
    )
    p.add_argument(
        "symbol",
        help="Perp (e.g. BTC) or spot pair (e.g. ETH/USDC), or numeric market_index",
    )
    p.add_argument("--amount", type=float, required=True, help="USDC amount")
    p.add_argument("--direction", required=True, choices=["add", "remove"])

    # -------------------------------------------------------------------------
    # funds <action>
    # -------------------------------------------------------------------------
    funds = sub.add_parser("funds", help="Fund operations")
    funds_sub = funds.add_subparsers(dest="action", required=True)

    p = funds_sub.add_parser(
        "withdraw",
        help="Withdraw assets",
        epilog=_WRITE_EPILOG,
    )
    p.add_argument("--asset", required=True, choices=list(ASSETS.keys()))
    p.add_argument("--amount", type=float, required=True)
    p.add_argument(
        "--route",
        default="perp",
        choices=["perp", "spot"],
        help="Withdraw from perp or spot balance (default: perp)",
    )

    p = funds_sub.add_parser(
        "transfer",
        help="Move an asset between spot and perp routes on the same account",
        epilog=_WRITE_EPILOG,
    )
    p.add_argument("--asset", required=True, choices=list(ASSETS.keys()))
    p.add_argument("--amount", type=float, required=True, help="Amount in human units")
    p.add_argument("--from_route", required=True, choices=["perp", "spot"])
    p.add_argument("--to_route", required=True, choices=["perp", "spot"])

    return parser


async def build_signer_client():
    """Build and validate a SignerClient from configured credentials."""
    api_private_key = get_config_value("LIGHTER_API_PRIVATE_KEY")
    if not api_private_key:
        error(
            "missing LIGHTER_API_PRIVATE_KEY; set it as an env var or in your "
            "lighter-agent-kit credentials file"
        )

    account_index_raw = get_config_value("LIGHTER_ACCOUNT_INDEX")
    if account_index_raw is None:
        error(
            "missing LIGHTER_ACCOUNT_INDEX; set it as an env var or in your "
            "lighter-agent-kit credentials file"
        )
    try:
        account_index = int(account_index_raw)
    except ValueError:
        error("LIGHTER_ACCOUNT_INDEX must be an integer")

    api_key_index_raw = get_config_value("LIGHTER_API_KEY_INDEX")
    if api_key_index_raw is None:
        error(
            "missing LIGHTER_API_KEY_INDEX; set it as an env var or in your "
            "lighter-agent-kit credentials file"
        )
    try:
        api_key_index = int(api_key_index_raw)
    except ValueError:
        error("LIGHTER_API_KEY_INDEX must be an integer")

    host = get_config_value("LIGHTER_HOST", DEFAULT_HOST)

    try:
        client = lighter.SignerClient(
            url=host,
            account_index=account_index,
            api_private_keys={api_key_index: api_private_key.expose()},
        )
        tag_api_client(client.api_client)
    except Exception as e:
        error(f"failed to initialize signer: {clean_sdk_error(str(e))}")

    try:
        err = client.check_client()
        if err is not None:
            await client.api_client.close()
            error(f"client check failed: {err}")
        return client
    except BaseException:
        await client.api_client.close()
        raise


async def fetch_market_decimals(client, market_index):
    """Return (size_decimals, price_decimals) for the given market."""
    result = await client.order_api.order_books()
    for ob in result.order_books:
        if ob.market_id == market_index:
            return ob.supported_size_decimals, ob.supported_price_decimals
    error(f"market_index {market_index} not found")


def scale(value, decimals):
    return int(round(value * (10**decimals)))


def format_scaled(scaled_int, decimals):
    """Format an integer-scaled value back to its human decimal form."""
    return f"{scaled_int / (10**decimals):.{decimals}f}"


def next_client_order_index():
    return int(time.time() * 1000) % (2**31)


def reserve_batch_nonces(client, tx_count, api_key_index=None):
    """Reserve sequential nonces across a batch on both old and new SDKs."""
    reserve = getattr(client, "reserve_batch_nonces", None)
    if callable(reserve):
        if api_key_index is None:
            return reserve(tx_count)
        return reserve(tx_count, api_key_index=api_key_index)

    if tx_count <= 0:
        raise ValueError("tx_count must be positive")

    nonce_manager = getattr(client, "nonce_manager", None)
    if nonce_manager is None:
        raise AttributeError("SignerClient nonce_manager is unavailable")

    default_api_key_index = getattr(client, "DEFAULT_API_KEY_INDEX", 255)
    reserved_api_key_index = (
        default_api_key_index if api_key_index is None else api_key_index
    )
    nonces = []
    for idx in range(tx_count):
        if idx == 0 and reserved_api_key_index == default_api_key_index:
            reserved_api_key_index, nonce = nonce_manager.next_nonce()
        else:
            reserved_api_key_index, nonce = nonce_manager.next_nonce(
                reserved_api_key_index
            )
        nonces.append(nonce)
    return reserved_api_key_index, nonces


def rollback_reserved_nonces(client, api_key_index, tx_count):
    rollback = getattr(client, "rollback_reserved_nonces", None)
    if callable(rollback):
        rollback(api_key_index, tx_count)
        return

    nonce_manager = getattr(client, "nonce_manager", None)
    if nonce_manager is None:
        return

    for _ in range(max(0, tx_count)):
        nonce_manager.acknowledge_failure(api_key_index)


async def send_tx_batch_with_nonce_management(client, tx_types, tx_infos, api_key_index):
    send_batch = getattr(client, "send_tx_batch_with_nonce_management", None)
    if callable(send_batch):
        return await send_batch(
            tx_types=tx_types,
            tx_infos=tx_infos,
            api_key_index=api_key_index,
        )

    try:
        response = await client.send_tx_batch(tx_types=tx_types, tx_infos=tx_infos)
    except Exception as exc:
        bad_request_exception = getattr(
            getattr(lighter, "exceptions", None),
            "BadRequestException",
            None,
        )
        if (
            bad_request_exception is not None
            and isinstance(exc, bad_request_exception)
            and "invalid nonce" in str(exc)
        ):
            nonce_manager = getattr(client, "nonce_manager", None)
            if nonce_manager is not None:
                nonce_manager.hard_refresh_nonce(api_key_index)
        else:
            rollback_reserved_nonces(client, api_key_index, len(tx_types))
        raise

    if getattr(response, "code", 200) != 200:
        rollback_reserved_nonces(client, api_key_index, len(tx_types))
    return response


def tx_response(tx, response):
    out = {"status": "submitted"}
    if response is not None and getattr(response, "tx_hash", None):
        out["tx_hash"] = response.tx_hash
    if tx is not None:
        try:
            if hasattr(tx, "to_json"):
                out["tx"] = json.loads(tx.to_json())
            elif isinstance(tx, str):
                out["tx"] = json.loads(tx)
        except Exception:
            pass
    return out


# -----------------------------------------------------------------------------
# Command handlers
# -----------------------------------------------------------------------------


async def cmd_order_market(client, args, market_index, market_type):
    if args.amount <= 0:
        error("--amount must be positive")

    size_dec, _ = await fetch_market_decimals(client, market_index)
    base_amount = scale(args.amount, size_dec)
    if base_amount <= 0:
        error("--amount too small for this market's size precision")

    normalized_side = normalize_side(args.side, market_type)
    is_ask = side_to_is_ask(normalized_side)
    coi = args.client_order_index or next_client_order_index()

    tx, response, err = await client.create_market_order_limited_slippage(
        market_index=market_index,
        client_order_index=coi,
        base_amount=base_amount,
        max_slippage=args.slippage,
        is_ask=is_ask,
        reduce_only=args.reduce_only,
    )
    if err is not None:
        error(f"order market failed: {clean_sdk_error(err)}")
    out = tx_response(tx, response)
    out["client_order_index"] = coi
    out["effective_amount"] = format_scaled(base_amount, size_dec)
    out["side"] = normalized_side
    output(out)


async def cmd_order_limit(client, args, market_index, market_type):
    if args.amount <= 0:
        error("--amount must be positive")

    size_dec, price_dec = await fetch_market_decimals(client, market_index)
    base_amount = scale(args.amount, size_dec)
    if base_amount <= 0:
        error("--amount too small for this market's size precision")

    normalized_side = normalize_side(args.side, market_type)
    is_ask = side_to_is_ask(normalized_side)
    coi = args.client_order_index or next_client_order_index()
    price = scale(args.price, price_dec)
    tif = (
        client.ORDER_TIME_IN_FORCE_POST_ONLY
        if args.post_only
        else client.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME
    )

    tx, response, err = await client.create_order(
        market_index=market_index,
        client_order_index=coi,
        base_amount=base_amount,
        price=price,
        is_ask=is_ask,
        order_type=client.ORDER_TYPE_LIMIT,
        time_in_force=tif,
        reduce_only=args.reduce_only,
    )
    if err is not None:
        error(f"order limit failed: {clean_sdk_error(err)}")
    out = tx_response(tx, response)
    out["client_order_index"] = coi
    out["effective_amount"] = format_scaled(base_amount, size_dec)
    out["effective_price"] = format_scaled(price, price_dec)
    out["side"] = normalized_side
    output(out)


async def cmd_order_modify(client, args, market_index, market_type):
    if args.amount <= 0 or args.price <= 0:
        error("--amount and --price must be positive")

    size_dec, price_dec = await fetch_market_decimals(client, market_index)
    base_amount = scale(args.amount, size_dec)
    price = scale(args.price, price_dec)

    tx, response, err = await client.modify_order(
        market_index=market_index,
        order_index=args.order_index,
        base_amount=base_amount,
        price=price,
    )
    if err is not None:
        error(f"order modify failed: {clean_sdk_error(err)}")
    out = tx_response(tx, response)
    out["effective_amount"] = format_scaled(base_amount, size_dec)
    out["effective_price"] = format_scaled(price, price_dec)
    output(out)


async def cmd_order_cancel(client, args, market_index, market_type):
    tx, response, err = await client.cancel_order(
        market_index=market_index,
        order_index=args.order_index,
    )
    if err is not None:
        error(f"order cancel failed: {clean_sdk_error(err)}")
    output(tx_response(tx, response))


async def cmd_order_cancel_all(client, args):
    tx, response, err = await client.cancel_all_orders(
        time_in_force=client.CANCEL_ALL_TIF_IMMEDIATE,
        timestamp_ms=0,
    )
    if err is not None:
        error(f"order cancel_all failed: {clean_sdk_error(err)}")
    output(tx_response(tx, response))


async def cmd_order_close_all(client, args):
    if args.slippage <= 0:
        error("--slippage must be positive")

    order_books = (await client.order_api.order_books()).order_books
    decimals_by_market = {
        ob.market_id: (ob.supported_size_decimals, ob.supported_price_decimals)
        for ob in order_books
    }
    symbol_by_market = {ob.market_id: ob.symbol for ob in order_books}

    account_api = lighter.AccountApi(client.api_client)
    acct = await account_api.account(by="index", value=str(client.account_index))
    positions = acct.accounts[0].positions if acct.accounts else []

    non_zero = []
    for p in positions:
        try:
            size = float(p.position)
        except (TypeError, ValueError):
            size = 0.0
        if size <= 0:
            continue
        non_zero.append(p)

    would_close = []
    for p in non_zero:
        size_dec, _ = decimals_by_market.get(p.market_id, (0, 0))
        size = float(p.position)
        is_long = int(p.sign) == 1
        would_close.append({
            "symbol": p.symbol or symbol_by_market.get(p.market_id),
            "market_id": p.market_id,
            "current_side": "long" if is_long else "short",
            "closing_side": "short" if is_long else "long",
            "amount": f"{size:.{size_dec}f}" if size_dec else str(size),
        })

    if args.preview:
        result = {
            "status": "ok",
            "preview": True,
            "would_close": would_close,
        }
        if args.with_cancel_all:
            result["note"] = (
                "--with_cancel_all would cancel all resting orders before "
                "sending the close batch"
            )
        output(result)
        return

    cancelled_orders_first = False
    cancel_all_tx_hash = None
    cancel_all_error = None
    closed = []
    failed = []
    pending_txs = []
    batch_api_key_index = None
    sign_specs = []

    for p in non_zero:
        size_dec, _ = decimals_by_market.get(p.market_id, (None, None))
        if size_dec is None:
            failed.append({
                "symbol": p.symbol,
                "market_id": p.market_id,
                "error": "market decimals not found",
            })
            continue

        size = float(p.position)
        base_amount = scale(size, size_dec)
        if base_amount <= 0:
            failed.append({
                "symbol": p.symbol,
                "market_id": p.market_id,
                "error": "position size rounds to zero at market precision",
            })
            continue

        is_long = int(p.sign) == 1
        is_ask = is_long  # long -> sell (ask), short -> buy (bid)
        closing_side = "short" if is_long else "long"
        coi = next_client_order_index()

        try:
            ideal_price = await client.get_best_price(p.market_id, is_ask)
            acceptable_execution_price = round(
                ideal_price * (1 + args.slippage * (-1 if is_ask else 1))
            )
        except Exception as exc:
            failed.append({
                "symbol": p.symbol,
                "market_id": p.market_id,
                "error": clean_sdk_error(str(exc)),
            })
            continue

        sign_specs.append({
            "kind": "close",
            "market_id": p.market_id,
            "client_order_index": coi,
            "base_amount": base_amount,
            "price": acceptable_execution_price,
            "is_ask": is_ask,
            "entry": {
                "symbol": p.symbol,
                "market_id": p.market_id,
                "closing_side": closing_side,
                "amount": format_scaled(base_amount, size_dec),
                "client_order_index": coi,
            },
        })

    if args.with_cancel_all:
        sign_specs.insert(0, {
            "kind": "cancel_all",
            "time_in_force": client.CANCEL_ALL_TIF_IMMEDIATE,
            "timestamp_ms": 0,
        })
        cancelled_orders_first = True

    if sign_specs:
        batch_api_key_index, nonces = reserve_batch_nonces(client, len(sign_specs))
        for spec, nonce in zip(sign_specs, nonces):
            if spec["kind"] == "cancel_all":
                cancel_tx_type, cancel_tx_info, cancel_tx_hash, cancel_err = client.sign_cancel_all_orders(
                    time_in_force=spec["time_in_force"],
                    timestamp_ms=spec["timestamp_ms"],
                    nonce=nonce,
                    api_key_index=batch_api_key_index,
                )
                if cancel_err is not None:
                    rollback_reserved_nonces(
                        client,
                        batch_api_key_index,
                        len(sign_specs),
                    )
                    cancel_all_error = clean_sdk_error(cancel_err)
                    pending_txs = []
                    break
                pending_txs.append({
                    "tx_type": cancel_tx_type,
                    "tx_info": cancel_tx_info,
                    "tx_hash": cancel_tx_hash,
                    "kind": "cancel_all",
                })
                continue

            tx_type, tx_info, signed_tx_hash, err = client.sign_create_order(
                market_index=spec["market_id"],
                client_order_index=spec["client_order_index"],
                base_amount=spec["base_amount"],
                price=spec["price"],
                is_ask=spec["is_ask"],
                order_type=client.ORDER_TYPE_MARKET,
                time_in_force=client.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
                order_expiry=client.DEFAULT_IOC_EXPIRY,
                reduce_only=True,
                nonce=nonce,
                api_key_index=batch_api_key_index,
            )
            if err is not None:
                rollback_reserved_nonces(
                    client,
                    batch_api_key_index,
                    len(sign_specs),
                )
                for prior in pending_txs:
                    if prior["kind"] == "close":
                        failed.append({
                            "symbol": prior["entry"]["symbol"],
                            "market_id": prior["entry"]["market_id"],
                            "error": "aborted before send: a later sign failed",
                        })
                failed.append({
                    "symbol": spec["entry"]["symbol"],
                    "market_id": spec["entry"]["market_id"],
                    "error": clean_sdk_error(err),
                })
                pending_txs = []
                break

            pending_txs.append({
                "tx_type": tx_type,
                "tx_info": tx_info,
                "tx_hash": signed_tx_hash,
                "kind": "close",
                "entry": spec["entry"],
            })

    if pending_txs:
        try:
            response = await send_tx_batch_with_nonce_management(
                client,
                tx_types=[tx["tx_type"] for tx in pending_txs],
                tx_infos=[tx["tx_info"] for tx in pending_txs],
                api_key_index=batch_api_key_index,
            )
        except Exception as exc:
            batch_error = clean_sdk_error(str(exc))
            for tx in pending_txs:
                if tx["kind"] == "close":
                    failed.append({
                        "symbol": tx["entry"]["symbol"],
                        "market_id": tx["entry"]["market_id"],
                        "error": batch_error,
                    })
                elif tx["kind"] == "cancel_all":
                    cancel_all_error = batch_error
        else:
            if getattr(response, "code", 200) != 200:
                batch_error = clean_sdk_error(getattr(response, "message", "") or "batch send failed")
                for tx in pending_txs:
                    if tx["kind"] == "close":
                        failed.append({
                            "symbol": tx["entry"]["symbol"],
                            "market_id": tx["entry"]["market_id"],
                            "error": batch_error,
                        })
                    elif tx["kind"] == "cancel_all":
                        cancel_all_error = batch_error
            else:
                batch_hashes = list(getattr(response, "tx_hash", []) or [])
                cursor = 0
                if pending_txs and pending_txs[0]["kind"] == "cancel_all":
                    cancel_all_tx_hash = (
                        batch_hashes[0] if batch_hashes else pending_txs[0].get("tx_hash")
                    )
                    cursor = 1

                for offset, tx in enumerate([item for item in pending_txs if item["kind"] == "close"]):
                    entry = dict(tx["entry"])
                    tx_hash = (
                        batch_hashes[cursor + offset]
                        if len(batch_hashes) > cursor + offset
                        else tx.get("tx_hash")
                    )
                    if tx_hash:
                        entry["tx_hash"] = tx_hash
                    closed.append(entry)

    any_failure = bool(failed) or cancel_all_error is not None
    if not closed and any_failure:
        status = "error"
    elif any_failure:
        status = "partial"
    else:
        status = "ok"

    result = {
        "status": status,
        "closed": closed,
        "failed": failed,
        "cancelled_orders_first": cancelled_orders_first,
    }
    if cancel_all_tx_hash:
        result["cancel_all_tx_hash"] = cancel_all_tx_hash
    if cancel_all_error:
        result["cancel_all_error"] = cancel_all_error

    if cancel_all_tx_hash and failed:
        result["warning"] = (
            f"--with_cancel_all cancelled every resting order before the close loop, "
            f"but {len(failed)} position(s) failed to close. Those markets are now "
            f"open with no TP/SL protection. Re-run `order close_all` for the "
            f"remaining positions, or re-place brackets manually."
        )

    output(result)


async def cmd_position_leverage(client, args, market_index, market_type):
    if args.leverage < 1:
        error("--leverage must be >= 1")

    margin_mode = (
        client.CROSS_MARGIN_MODE
        if args.margin_mode == "cross"
        else client.ISOLATED_MARGIN_MODE
    )

    tx, response, err = await client.update_leverage(
        market_index=market_index,
        margin_mode=margin_mode,
        leverage=args.leverage,
    )
    if err is not None:
        error(f"position leverage failed: {clean_sdk_error(err)}")
    output(tx_response(tx, response))


async def cmd_position_margin(client, args, market_index, market_type):
    if args.amount <= 0:
        error("--amount must be positive")

    direction = (
        client.ISOLATED_MARGIN_ADD_COLLATERAL
        if args.direction == "add"
        else client.ISOLATED_MARGIN_REMOVE_COLLATERAL
    )

    tx, response, err = await client.update_margin(
        market_index=market_index,
        usdc_amount=args.amount,
        direction=direction,
    )
    if err is not None:
        error(f"position margin failed: {clean_sdk_error(err)}")
    output(tx_response(tx, response))


async def cmd_funds_withdraw(client, args):
    if args.amount <= 0:
        error("--amount must be positive")

    asset_id = getattr(client, ASSETS[args.asset])
    route_type = client.ROUTE_PERP if args.route == "perp" else client.ROUTE_SPOT

    tx, response, err = await client.withdraw(
        asset_id=asset_id,
        route_type=route_type,
        amount=args.amount,
    )
    if err is not None:
        error(f"funds withdraw failed: {clean_sdk_error(err)}")
    output(tx_response(tx, response))


async def cmd_funds_transfer(client, args):
    if args.amount <= 0:
        error("--amount must be positive")
    if args.from_route == args.to_route:
        error("--from_route and --to_route must differ")

    asset_id = getattr(client, ASSETS[args.asset])
    route_from = client.ROUTE_PERP if args.from_route == "perp" else client.ROUTE_SPOT
    route_to = client.ROUTE_PERP if args.to_route == "perp" else client.ROUTE_SPOT

    tx, response, err = await client.transfer_same_master_account(
        to_account_index=client.account_index,
        asset_id=asset_id,
        route_from=route_from,
        route_to=route_to,
        amount=args.amount,
        fee=0,
        memo="0" * 64,
    )
    if err is not None:
        error(f"funds transfer failed: {clean_sdk_error(err)}")
    output(tx_response(tx, response))


# Commands that need symbol resolution
SYMBOL_COMMANDS = {
    ("order", "market"): cmd_order_market,
    ("order", "limit"): cmd_order_limit,
    ("order", "modify"): cmd_order_modify,
    ("order", "cancel"): cmd_order_cancel,
    ("position", "leverage"): cmd_position_leverage,
    ("position", "margin"): cmd_position_margin,
}

# Commands without symbol resolution
SIMPLE_COMMANDS = {
    ("order", "cancel_all"): cmd_order_cancel_all,
    ("order", "close_all"): cmd_order_close_all,
    ("funds", "withdraw"): cmd_funds_withdraw,
    ("funds", "transfer"): cmd_funds_transfer,
}


async def run(args):
    host = get_config_value("LIGHTER_HOST", DEFAULT_HOST)
    key = (args.group, args.action)

    try:
        client = await build_signer_client()
    except Exception as e:
        error(f"failed to initialize signer: {clean_sdk_error(str(e))}")

    try:
        if key in SIMPLE_COMMANDS:
            await SIMPLE_COMMANDS[key](client, args)
        elif key in SYMBOL_COMMANDS:
            try:
                market_index, market_type, _ = await resolve_symbol(
                    args.symbol,
                    host,
                    client.api_client,
                )
            except ValueError as e:
                error(str(e))
            await SYMBOL_COMMANDS[key](client, args, market_index, market_type)
        else:
            error(f"unknown command: {args.group} {args.action}")
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
    finally:
        await client.api_client.close()


def main():
    args = build_parser().parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
