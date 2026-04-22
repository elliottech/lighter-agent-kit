"""Offline tests for live-trading command orchestration."""

import asyncio
import importlib.util
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

    symbols_mod = types.ModuleType("_symbols")
    symbols_mod.normalize_side = lambda side, market_type: side
    symbols_mod.resolve_symbol = lambda *args, **kwargs: None
    symbols_mod.side_to_is_ask = lambda side: side in {"sell", "short"}

    lighter_mod = types.ModuleType("lighter")

    class BadRequestException(Exception):
        pass

    lighter_mod.exceptions = types.SimpleNamespace(BadRequestException=BadRequestException)

    module_name = f"trade_test_{uuid.uuid4().hex}"
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

    def __init__(self, *, positions, books, best_prices, batch_response=None, batch_error=None):
        self.account_index = 42
        self.api_client = types.SimpleNamespace(
            account_response=types.SimpleNamespace(
                accounts=[types.SimpleNamespace(positions=positions)]
            )
        )
        self.order_api = types.SimpleNamespace(
            order_books=self._order_books,
        )
        self._books = books
        self._best_prices = best_prices
        self.batch_response = batch_response
        self.batch_error = batch_error
        self.nonce_manager = _NonceManager()
        self.signed_create_orders = []
        self.signed_cancel_all = []
        self.sent_batches = []

    async def _order_books(self):
        return types.SimpleNamespace(order_books=self._books)

    async def get_best_price(self, market_index, is_ask):
        return self._best_prices[(market_index, is_ask)]

    def sign_cancel_all_orders(self, **kwargs):
        self.signed_cancel_all.append(kwargs)
        return 9, json.dumps({"kind": "cancel_all", "nonce": kwargs["nonce"]}), "signed-cancel-all", None

    def sign_create_order(self, **kwargs):
        self.signed_create_orders.append(kwargs)
        return (
            1,
            json.dumps(
                {
                    "kind": "close",
                    "market_index": kwargs["market_index"],
                    "nonce": kwargs["nonce"],
                }
            ),
            f"signed-{kwargs['market_index']}-{kwargs['nonce']}",
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
        return self.batch_response

    async def send_tx_batch_with_nonce_management(self, tx_types, tx_infos, api_key_index):
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


class _LegacyBatchClient(_FakeClient):
    """Simulate the released SDK, which lacks the new batch helper methods."""

    def __getattribute__(self, name):
        if name in {
            "reserve_batch_nonces",
            "rollback_reserved_nonces",
            "send_tx_batch_with_nonce_management",
        }:
            raise AttributeError(name)
        return super().__getattribute__(name)


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


def test_tx_response_uses_submitted_status_for_simple_writes(trade_module):
    tx = types.SimpleNamespace(
        to_json=lambda: json.dumps({"AccountIndex": 42, "Nonce": 7})
    )
    response = types.SimpleNamespace(tx_hash="abc123")

    result = trade_module.tx_response(tx, response)

    assert result == {
        "status": "submitted",
        "tx_hash": "abc123",
        "tx": {
            "AccountIndex": 42,
            "Nonce": 7,
        },
    }


def test_close_all_uses_send_tx_batch(trade_module):
    books = [
        types.SimpleNamespace(
            market_id=1,
            supported_size_decimals=4,
            supported_price_decimals=2,
            symbol="BTC",
        ),
        types.SimpleNamespace(
            market_id=2,
            supported_size_decimals=3,
            supported_price_decimals=2,
            symbol="ETH",
        ),
    ]
    positions = [
        types.SimpleNamespace(market_id=1, symbol="BTC", position="0.5000", sign=1),
        types.SimpleNamespace(market_id=2, symbol="ETH", position="1.250", sign=0),
    ]
    client = _FakeClient(
        positions=positions,
        books=books,
        best_prices={
            (1, True): 100_00,
            (2, False): 200_00,
        },
        batch_response=types.SimpleNamespace(
            code=200,
            tx_hash=["batch-cancel", "batch-btc", "batch-eth"],
        ),
    )
    client._test_bad_request_exception = trade_module._test_bad_request_exception
    args = types.SimpleNamespace(slippage=0.01, preview=False, with_cancel_all=True)

    asyncio.run(trade_module.cmd_order_close_all(client, args))

    assert len(client.sent_batches) == 1
    tx_types, tx_infos = client.sent_batches[0]
    assert tx_types == [9, 1, 1]
    assert [json.loads(tx)["nonce"] for tx in tx_infos] == [100, 101, 102]
    assert client.nonce_manager.next_nonce_calls == [None, 7, 7]

    result = trade_module._test_outputs[-1]
    assert result["status"] == "ok"
    assert result["cancelled_orders_first"] is True
    assert result["cancel_all_tx_hash"] == "batch-cancel"
    assert [entry["tx_hash"] for entry in result["closed"]] == ["batch-btc", "batch-eth"]
    assert [entry["closing_side"] for entry in result["closed"]] == ["short", "long"]


def test_close_all_rolls_back_reserved_nonces_on_batch_failure(trade_module):
    books = [
        types.SimpleNamespace(
            market_id=1,
            supported_size_decimals=4,
            supported_price_decimals=2,
            symbol="BTC",
        )
    ]
    positions = [
        types.SimpleNamespace(market_id=1, symbol="BTC", position="0.5000", sign=1),
    ]
    client = _FakeClient(
        positions=positions,
        books=books,
        best_prices={(1, True): 100_00},
        batch_error=Exception("message='temporary overload'"),
    )
    client._test_bad_request_exception = trade_module._test_bad_request_exception
    args = types.SimpleNamespace(slippage=0.01, preview=False, with_cancel_all=False)

    asyncio.run(trade_module.cmd_order_close_all(client, args))

    result = trade_module._test_outputs[-1]
    assert result["status"] == "error"
    assert result["closed"] == []
    assert result["failed"] == [
        {
            "symbol": "BTC",
            "market_id": 1,
            "error": "temporary overload",
        }
    ]
    assert client.nonce_manager.ack_count == 1
    assert client.nonce_manager.hard_refresh_calls == []


def test_close_all_uses_local_batch_helpers_when_sdk_lacks_them(trade_module):
    books = [
        types.SimpleNamespace(
            market_id=1,
            supported_size_decimals=4,
            supported_price_decimals=2,
            symbol="BTC",
        ),
        types.SimpleNamespace(
            market_id=2,
            supported_size_decimals=3,
            supported_price_decimals=2,
            symbol="ETH",
        ),
    ]
    positions = [
        types.SimpleNamespace(market_id=1, symbol="BTC", position="0.5000", sign=1),
        types.SimpleNamespace(market_id=2, symbol="ETH", position="1.250", sign=0),
    ]
    client = _LegacyBatchClient(
        positions=positions,
        books=books,
        best_prices={
            (1, True): 100_00,
            (2, False): 200_00,
        },
        batch_response=types.SimpleNamespace(
            code=200,
            tx_hash=["batch-cancel", "batch-btc", "batch-eth"],
        ),
    )
    client._test_bad_request_exception = trade_module._test_bad_request_exception
    args = types.SimpleNamespace(slippage=0.01, preview=False, with_cancel_all=True)

    asyncio.run(trade_module.cmd_order_close_all(client, args))

    assert len(client.sent_batches) == 1
    tx_types, tx_infos = client.sent_batches[0]
    assert tx_types == [9, 1, 1]
    assert [json.loads(tx)["nonce"] for tx in tx_infos] == [100, 101, 102]
    assert client.nonce_manager.next_nonce_calls == [None, 7, 7]

    result = trade_module._test_outputs[-1]
    assert result["status"] == "ok"
    assert result["cancelled_orders_first"] is True
    assert result["cancel_all_tx_hash"] == "batch-cancel"
    assert [entry["tx_hash"] for entry in result["closed"]] == ["batch-btc", "batch-eth"]
