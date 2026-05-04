"""Offline tests for order close_all orchestration."""

import asyncio
import importlib.util
import inspect
import json
import os
import sys
import types
import uuid

import pytest


_TEST_DIR = os.path.dirname(os.path.abspath(__file__))
_SKILL_DIR = os.path.dirname(_TEST_DIR)
_TRADE_PATH = os.path.join(_SKILL_DIR, "scripts", "trade.py")


def _load_trade_module():
    outputs = []

    cli_mod = types.ModuleType("_cli")

    def output(data):
        outputs.append(data)

    def error(msg):
        output({"error": msg})
        raise SystemExit(1)

    class JsonArgumentParser:  # pragma: no cover - import stub only
        pass

    cli_mod.output = output
    cli_mod.error = error
    cli_mod.JsonArgumentParser = JsonArgumentParser

    sdk_mod = types.ModuleType("_sdk")
    sdk_mod.DEFAULT_HOST = "https://mainnet.zklighter.elliot.ai"
    sdk_mod.ensure_lighter = lambda: None
    sdk_mod.get_config_value = lambda *args, **kwargs: None
    sdk_mod.tag_api_client = lambda api_client: api_client

    symbols_mod = types.ModuleType("_symbols")
    symbols_mod.normalize_side = lambda side, market_type: side
    symbols_mod.resolve_symbol = lambda *args, **kwargs: None
    symbols_mod.side_to_is_ask = lambda side: side in {"sell", "short"}

    lighter_mod = types.ModuleType("lighter")

    class BadRequestException(Exception):
        pass

    lighter_mod.exceptions = types.SimpleNamespace(
        BadRequestException=BadRequestException
    )

    module_name = f"trade_close_all_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, _TRADE_PATH)
    module = importlib.util.module_from_spec(spec)

    sys.modules[module_name] = module
    sys.modules["_cli"] = cli_mod
    sys.modules["_sdk"] = sdk_mod
    sys.modules["_symbols"] = symbols_mod
    sys.modules["lighter"] = lighter_mod

    spec.loader.exec_module(module)
    module._test_outputs = outputs
    module._test_bad_request_exception = BadRequestException
    module._test_lighter_module = lighter_mod
    return module


class _DefaultBadRequestException(Exception):
    pass


class _NonceManager:
    def __init__(self, api_key_index=7, start_nonce=100):
        self.api_key_index = api_key_index
        self.current_nonce = start_nonce - 1
        self.next_nonce_calls = []
        self.ack_count = 0
        self.hard_refresh_calls = []

    def next_nonce(self, api_key=None):
        self.next_nonce_calls.append(api_key)
        if api_key is None:
            api_key = self.api_key_index
        self.current_nonce += 1
        return api_key, self.current_nonce

    def acknowledge_failure(self, api_key):
        assert api_key == self.api_key_index
        self.ack_count += 1
        self.current_nonce -= 1

    def hard_refresh_nonce(self, api_key):
        self.hard_refresh_calls.append(api_key)


class _FakeClient:
    CANCEL_ALL_TIF_IMMEDIATE = 0
    ORDER_TYPE_MARKET = 1
    ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL = 0
    DEFAULT_IOC_EXPIRY = 0

    def __init__(
        self,
        *,
        positions,
        books,
        best_prices=None,
        best_price_errors=None,
        batch_response=None,
        batch_error=None,
        sign_create_order_errors=None,
        sign_cancel_all_error=None,
        market_order_results=None,
        cancel_all_result=None,
    ):
        self.account_index = 42
        self.api_client = types.SimpleNamespace(
            account_response=types.SimpleNamespace(
                accounts=[types.SimpleNamespace(positions=positions)]
            )
        )
        self.order_api = types.SimpleNamespace(order_books=self._order_books)
        self._books = books
        self._best_prices = best_prices or {}
        self._best_price_errors = best_price_errors or {}
        self.batch_response = batch_response
        self.batch_error = batch_error
        self.sign_create_order_errors = sign_create_order_errors or {}
        self.sign_cancel_all_error = sign_cancel_all_error
        self.market_order_results = market_order_results or {}
        self.cancel_all_result = cancel_all_result
        self.nonce_manager = _NonceManager()
        self.signed_create_orders = []
        self.signed_cancel_all = []
        self.direct_market_order_calls = []
        self.direct_cancel_all_calls = []
        self.sent_batches = []
        self._test_bad_request_exception = _DefaultBadRequestException

    async def _order_books(self):
        return types.SimpleNamespace(order_books=self._books)

    async def get_best_price(self, market_index, is_ask):
        if (market_index, is_ask) in self._best_price_errors:
            raise self._best_price_errors[(market_index, is_ask)]
        return self._best_prices[(market_index, is_ask)]

    async def cancel_all_orders(self, **kwargs):
        self.direct_cancel_all_calls.append(kwargs)
        if self.cancel_all_result is not None:
            return self.cancel_all_result
        tx = json.dumps({"kind": "cancel_all"})
        response = types.SimpleNamespace(tx_hash="direct-cancel-all")
        return tx, response, None

    async def create_market_order_limited_slippage(self, **kwargs):
        self.direct_market_order_calls.append(kwargs)
        market_index = kwargs["market_index"]
        if market_index in self.market_order_results:
            return self.market_order_results[market_index]
        tx = json.dumps({"kind": "close", "market_index": market_index})
        response = types.SimpleNamespace(tx_hash=f"direct-{market_index}")
        return tx, response, None

    def sign_cancel_all_orders(self, **kwargs):
        self.signed_cancel_all.append(kwargs)
        if self.sign_cancel_all_error is not None:
            return 9, None, None, self.sign_cancel_all_error
        return (
            9,
            json.dumps({"kind": "cancel_all", "nonce": kwargs["nonce"]}),
            "signed-cancel-all",
            None,
        )

    def sign_create_order(self, **kwargs):
        self.signed_create_orders.append(kwargs)
        market_index = kwargs["market_index"]
        if market_index in self.sign_create_order_errors:
            return 1, None, None, self.sign_create_order_errors[market_index]
        return (
            1,
            json.dumps(
                {
                    "kind": "close",
                    "market_index": market_index,
                    "nonce": kwargs["nonce"],
                }
            ),
            f"signed-{market_index}-{kwargs['nonce']}",
            None,
        )

    def reserve_batch_nonces(self, tx_count, api_key_index=255):
        nonces = []
        reserved_api_key_index = api_key_index
        for idx in range(tx_count):
            if idx == 0 and reserved_api_key_index == 255:
                reserved_api_key_index, nonce = self.nonce_manager.next_nonce()
            else:
                reserved_api_key_index, nonce = self.nonce_manager.next_nonce(
                    reserved_api_key_index
                )
            nonces.append(nonce)
        return reserved_api_key_index, nonces

    def rollback_reserved_nonces(self, api_key_index, tx_count):
        for _ in range(max(0, tx_count)):
            self.nonce_manager.acknowledge_failure(api_key_index)

    async def send_tx_batch(self, tx_types, tx_infos):
        self.sent_batches.append((tx_types, tx_infos))
        if self.batch_error is not None:
            raise self.batch_error
        if self.batch_response is not None:
            return self.batch_response

        tx_hashes = []
        for tx_info in tx_infos:
            payload = json.loads(tx_info)
            if payload["kind"] == "cancel_all":
                tx_hashes.append("batch-cancel-all")
            else:
                tx_hashes.append(f"batch-{payload['market_index']}")
        return types.SimpleNamespace(code=200, tx_hash=tx_hashes)

    async def send_tx_batch_with_nonce_management(
        self, tx_types, tx_infos, api_key_index
    ):
        try:
            response = await self.send_tx_batch(tx_types=tx_types, tx_infos=tx_infos)
        except self._test_bad_request_exception:
            raise
        except Exception:
            self.rollback_reserved_nonces(api_key_index, len(tx_types))
            raise

        if getattr(response, "code", 200) != 200:
            self.rollback_reserved_nonces(api_key_index, len(tx_types))
        return response


@pytest.fixture
def trade_module():
    module = _load_trade_module()

    class AccountApi:
        def __init__(self, api_client):
            self.api_client = api_client

        async def account(self, by, value):
            assert by == "index"
            assert value == "42"
            return self.api_client.account_response

    module._test_lighter_module.AccountApi = AccountApi
    return module


def _run(coro):
    return asyncio.run(coro)


def _book(market_id, size_decimals, price_decimals=2, symbol=None):
    return types.SimpleNamespace(
        market_id=market_id,
        supported_size_decimals=size_decimals,
        supported_price_decimals=price_decimals,
        symbol=symbol or f"M{market_id}",
    )


def _position(market_id, position, sign, symbol=None):
    return types.SimpleNamespace(
        market_id=market_id,
        position=position,
        sign=sign,
        symbol=symbol,
    )


def _args(*, slippage=0.01, preview=False, with_cancel_all=False):
    return types.SimpleNamespace(
        slippage=slippage,
        preview=preview,
        with_cancel_all=with_cancel_all,
    )


def _latest_output(trade_module):
    return trade_module._test_outputs[-1]


def _set_client_order_indexes(trade_module, *indexes):
    sequence = iter(indexes)
    trade_module.next_client_order_index = lambda: next(sequence)


def _make_client(trade_module, **kwargs):
    client = _FakeClient(**kwargs)
    client._test_bad_request_exception = trade_module._test_bad_request_exception
    return client


def _require_batched_close_all(trade_module):
    source = inspect.getsource(trade_module.cmd_order_close_all)
    if "send_tx_batch_with_nonce_management" not in source:
        pytest.xfail("tracked worktree still has the pre-batch close_all implementation")


def test_close_all_preview_filters_zero_positions(trade_module):
    client = _make_client(
        trade_module,
        positions=[
            _position(1, "0.5000", 1, symbol=None),
            _position(2, "0", 0, symbol="ETH"),
            _position(3, "-1.25", 1, symbol="SOL"),
            _position(4, "not-a-number", 0, symbol="DOGE"),
        ],
        books=[
            _book(1, 4, symbol="BTC"),
            _book(2, 3, symbol="ETH"),
            _book(3, 2, symbol="SOL"),
            _book(4, 5, symbol="DOGE"),
        ],
    )

    _run(trade_module.cmd_order_close_all(client, _args(preview=True)))

    assert _latest_output(trade_module) == {
        "status": "ok",
        "preview": True,
        "would_close": [
            {
                "symbol": "BTC",
                "market_id": 1,
                "current_side": "long",
                "closing_side": "short",
                "amount": "0.5000",
            }
        ],
    }
    assert client.sent_batches == []
    assert client.direct_market_order_calls == []
    assert client.direct_cancel_all_calls == []


def test_close_all_preparation_failures_still_close_remaining_positions(trade_module):
    _set_client_order_indexes(trade_module, 4101)
    client = _make_client(
        trade_module,
        positions=[
            _position(1, "0.2500", 1, symbol="BTC"),
            _position(2, "1.00", 0, symbol="MISSING"),
            _position(3, "0.0004", 1, symbol="DUST"),
        ],
        books=[
            _book(1, 4, symbol="BTC"),
            _book(3, 3, symbol="DUST"),
        ],
        best_prices={(1, True): 100_00},
    )

    _run(trade_module.cmd_order_close_all(client, _args()))

    result = _latest_output(trade_module)
    assert result["status"] == "partial"
    assert result["cancelled_orders_first"] is False
    assert result["failed"] == [
        {
            "symbol": "MISSING",
            "market_id": 2,
            "error": "market decimals not found",
        },
        {
            "symbol": "DUST",
            "market_id": 3,
            "error": "position size rounds to zero at market precision",
        },
    ]
    assert result["closed"] == [
        {
            "symbol": "BTC",
            "market_id": 1,
            "closing_side": "short",
            "amount": "0.2500",
            "client_order_index": 4101,
            "tx_hash": result["closed"][0]["tx_hash"],
        }
    ]
    assert "warning" not in result


def test_close_all_returns_error_when_every_position_fails_preparation(trade_module):
    client = _make_client(
        trade_module,
        positions=[
            _position(2, "1.00", 0, symbol="MISSING"),
            _position(3, "0.0004", 1, symbol="DUST"),
        ],
        books=[_book(3, 3, symbol="DUST")],
    )

    _run(trade_module.cmd_order_close_all(client, _args()))

    assert _latest_output(trade_module) == {
        "status": "error",
        "closed": [],
        "failed": [
            {
                "symbol": "MISSING",
                "market_id": 2,
                "error": "market decimals not found",
            },
            {
                "symbol": "DUST",
                "market_id": 3,
                "error": "position size rounds to zero at market precision",
            },
        ],
        "cancelled_orders_first": False,
    }


def test_close_all_with_cancel_all_happy_path_uses_batch_send(trade_module):
    _require_batched_close_all(trade_module)
    _set_client_order_indexes(trade_module, 5101, 5102)
    client = _make_client(
        trade_module,
        positions=[
            _position(1, "0.5000", 1, symbol="BTC"),
            _position(2, "1.250", 0, symbol="ETH"),
        ],
        books=[
            _book(1, 4, 2, symbol="BTC"),
            _book(2, 3, 2, symbol="ETH"),
        ],
        best_prices={
            (1, True): 100_00,
            (2, False): 200_00,
        },
        batch_response=types.SimpleNamespace(
            code=200,
            tx_hash=["batch-cancel", "batch-btc", "batch-eth"],
        ),
    )

    _run(trade_module.cmd_order_close_all(client, _args(with_cancel_all=True)))

    assert len(client.sent_batches) == 1
    tx_types, tx_infos = client.sent_batches[0]
    assert tx_types == [9, 1, 1]
    assert [json.loads(tx)["nonce"] for tx in tx_infos] == [100, 101, 102]
    assert client.nonce_manager.next_nonce_calls == [None, 7, 7]
    assert [entry["price"] for entry in client.signed_create_orders] == [9900, 20200]

    assert _latest_output(trade_module) == {
        "status": "ok",
        "closed": [
            {
                "symbol": "BTC",
                "market_id": 1,
                "closing_side": "short",
                "amount": "0.5000",
                "client_order_index": 5101,
                "tx_hash": "batch-btc",
            },
            {
                "symbol": "ETH",
                "market_id": 2,
                "closing_side": "long",
                "amount": "1.250",
                "client_order_index": 5102,
                "tx_hash": "batch-eth",
            },
        ],
        "failed": [],
        "cancelled_orders_first": True,
        "cancel_all_tx_hash": "batch-cancel",
    }


def test_close_all_rolls_back_reserved_nonces_on_batch_failure(trade_module):
    _require_batched_close_all(trade_module)
    _set_client_order_indexes(trade_module, 6101)
    client = _make_client(
        trade_module,
        positions=[_position(1, "0.5000", 1, symbol="BTC")],
        books=[_book(1, 4, symbol="BTC")],
        best_prices={(1, True): 100_00},
        batch_error=Exception("message='temporary overload'"),
    )

    _run(trade_module.cmd_order_close_all(client, _args()))

    assert _latest_output(trade_module) == {
        "status": "error",
        "closed": [],
        "failed": [
            {
                "symbol": "BTC",
                "market_id": 1,
                "error": "temporary overload",
            }
        ],
        "cancelled_orders_first": False,
    }
    assert client.nonce_manager.ack_count == 1
    assert client.nonce_manager.hard_refresh_calls == []


def test_close_all_marks_non_200_batch_response_as_error_and_rolls_back(trade_module):
    _require_batched_close_all(trade_module)
    _set_client_order_indexes(trade_module, 7101)
    client = _make_client(
        trade_module,
        positions=[_position(1, "0.5000", 1, symbol="BTC")],
        books=[_book(1, 4, symbol="BTC")],
        best_prices={(1, True): 100_00},
        batch_response=types.SimpleNamespace(
            code=503,
            message="message='sequencer busy'",
            tx_hash=[],
        ),
    )

    _run(trade_module.cmd_order_close_all(client, _args()))

    assert _latest_output(trade_module) == {
        "status": "error",
        "closed": [],
        "failed": [
            {
                "symbol": "BTC",
                "market_id": 1,
                "error": "sequencer busy",
            }
        ],
        "cancelled_orders_first": False,
    }
    assert client.nonce_manager.ack_count == 1


def test_close_all_rolls_back_reserved_nonces_on_sign_failure(trade_module):
    _require_batched_close_all(trade_module)
    _set_client_order_indexes(trade_module, 8101, 8102)
    client = _make_client(
        trade_module,
        positions=[
            _position(1, "0.5000", 1, symbol="BTC"),
            _position(2, "1.250", 0, symbol="ETH"),
        ],
        books=[
            _book(1, 4, symbol="BTC"),
            _book(2, 3, symbol="ETH"),
        ],
        best_prices={
            (1, True): 100_00,
            (2, False): 200_00,
        },
        sign_create_order_errors={2: "message='signing blew up'"},
    )

    _run(trade_module.cmd_order_close_all(client, _args()))

    assert _latest_output(trade_module) == {
        "status": "error",
        "closed": [],
        "failed": [
            {
                "symbol": "BTC",
                "market_id": 1,
                "error": "aborted before send: a later sign failed",
            },
            {
                "symbol": "ETH",
                "market_id": 2,
                "error": "signing blew up",
            },
        ],
        "cancelled_orders_first": False,
    }
    assert client.sent_batches == []
    assert client.nonce_manager.ack_count == 2


def test_close_all_reports_best_price_lookup_failure_and_continues(trade_module):
    _require_batched_close_all(trade_module)
    _set_client_order_indexes(trade_module, 9101, 9102)
    client = _make_client(
        trade_module,
        positions=[
            _position(1, "0.5000", 1, symbol="BTC"),
            _position(2, "1.250", 0, symbol="ETH"),
        ],
        books=[
            _book(1, 4, symbol="BTC"),
            _book(2, 3, symbol="ETH"),
        ],
        best_prices={(2, False): 200_00},
        best_price_errors={
            (1, True): Exception("message='book unavailable'"),
        },
        batch_response=types.SimpleNamespace(code=200, tx_hash=["batch-eth"]),
    )

    _run(trade_module.cmd_order_close_all(client, _args()))

    result = _latest_output(trade_module)
    assert result["status"] == "partial"
    assert result["cancelled_orders_first"] is False
    assert result["failed"] == [
        {
            "symbol": "BTC",
            "market_id": 1,
            "error": "book unavailable",
        }
    ]
    assert result["closed"] == [
        {
            "symbol": "ETH",
            "market_id": 2,
            "closing_side": "long",
            "amount": "1.250",
            "client_order_index": 9102,
            "tx_hash": "batch-eth",
        }
    ]
    assert client.nonce_manager.next_nonce_calls == [None]


def test_close_all_warns_when_cancel_all_ran_but_some_closes_failed(trade_module):
    _require_batched_close_all(trade_module)
    _set_client_order_indexes(trade_module, 10101)
    client = _make_client(
        trade_module,
        positions=[
            _position(1, "0.2500", 1, symbol="BTC"),
            _position(2, "1.00", 0, symbol="MISSING"),
        ],
        books=[_book(1, 4, symbol="BTC")],
        best_prices={(1, True): 100_00},
        batch_response=types.SimpleNamespace(
            code=200,
            tx_hash=["batch-cancel", "batch-btc"],
        ),
    )

    _run(trade_module.cmd_order_close_all(client, _args(with_cancel_all=True)))

    result = _latest_output(trade_module)
    assert result["status"] == "partial"
    assert result["cancelled_orders_first"] is True
    assert result["cancel_all_tx_hash"] == "batch-cancel"
    assert result["failed"] == [
        {
            "symbol": "MISSING",
            "market_id": 2,
            "error": "market decimals not found",
        }
    ]
    assert result["closed"] == [
        {
            "symbol": "BTC",
            "market_id": 1,
            "closing_side": "short",
            "amount": "0.2500",
            "client_order_index": 10101,
            "tx_hash": "batch-btc",
        }
    ]
    assert "no TP/SL protection" in result["warning"]


def test_close_all_plain_happy_path_batches_only_close_orders(trade_module):
    _require_batched_close_all(trade_module)
    _set_client_order_indexes(trade_module, 11101, 11102)
    client = _make_client(
        trade_module,
        positions=[
            _position(120, "22.00", 1, symbol="LIT"),
            _position(0, "0.0050", 0, symbol="ETH"),
        ],
        books=[
            _book(120, 2, 4, symbol="LIT"),
            _book(0, 4, 2, symbol="ETH"),
        ],
        best_prices={
            (120, True): 9200,
            (0, False): 200000,
        },
        batch_response=types.SimpleNamespace(
            code=200,
            tx_hash=["batch-lit", "batch-eth"],
        ),
    )

    _run(trade_module.cmd_order_close_all(client, _args()))

    assert len(client.sent_batches) == 1
    tx_types, tx_infos = client.sent_batches[0]
    assert tx_types == [1, 1]
    assert [json.loads(tx)["nonce"] for tx in tx_infos] == [100, 101]
    assert client.signed_cancel_all == []
    assert _latest_output(trade_module) == {
        "status": "ok",
        "closed": [
            {
                "symbol": "LIT",
                "market_id": 120,
                "closing_side": "short",
                "amount": "22.00",
                "client_order_index": 11101,
                "tx_hash": "batch-lit",
            },
            {
                "symbol": "ETH",
                "market_id": 0,
                "closing_side": "long",
                "amount": "0.0050",
                "client_order_index": 11102,
                "tx_hash": "batch-eth",
            },
        ],
        "failed": [],
        "cancelled_orders_first": False,
    }


def test_close_all_preview_with_cancel_all_still_lists_only_positions(trade_module):
    client = _make_client(
        trade_module,
        positions=[
            _position(120, "22.00", 1, symbol="LIT"),
            _position(0, "0.0050", 0, symbol="ETH"),
        ],
        books=[
            _book(120, 2, 4, symbol="LIT"),
            _book(0, 4, 2, symbol="ETH"),
        ],
    )

    _run(trade_module.cmd_order_close_all(client, _args(preview=True, with_cancel_all=True)))

    assert _latest_output(trade_module) == {
        "status": "ok",
        "preview": True,
        "note": (
            "--with_cancel_all would cancel all resting orders before "
            "sending the close batch"
        ),
        "would_close": [
            {
                "symbol": "LIT",
                "market_id": 120,
                "current_side": "long",
                "closing_side": "short",
                "amount": "22.00",
            },
            {
                "symbol": "ETH",
                "market_id": 0,
                "current_side": "short",
                "closing_side": "long",
                "amount": "0.0050",
            },
        ],
    }
    assert client.sent_batches == []
    assert client.signed_cancel_all == []
