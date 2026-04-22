"""Offline tests for shared symbol resolution cache."""

import asyncio
import json
import os
import sys

import pytest


_TEST_DIR = os.path.dirname(os.path.abspath(__file__))
_SKILL_DIR = os.path.dirname(_TEST_DIR)
_SCRIPTS_DIR = os.path.join(_SKILL_DIR, "scripts")
sys.path.insert(0, _SCRIPTS_DIR)

import _symbols  # noqa: E402
from _paths import symbol_cache_path  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolate_symbol_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _symbols._LIVE_CACHE.clear()


def test_resolve_symbol_writes_shared_disk_cache(monkeypatch):
    calls = {"count": 0}

    async def fake_fetch(_api_client):
        calls["count"] += 1
        return {"perp": {"XAG": 77}, "spot": {"ETH/USDC": 2048}}

    monkeypatch.setattr(_symbols, "_fetch_symbols", fake_fetch)

    resolved = _run(
        _symbols.resolve_symbol("XAG", "https://mainnet.zklighter.elliot.ai", object())
    )

    assert resolved == (77, "perp", "XAG")
    assert calls["count"] == 1

    cache_path = symbol_cache_path("https://mainnet.zklighter.elliot.ai")
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert payload["host"] == "https://mainnet.zklighter.elliot.ai"
    assert payload["symbols"]["perp"]["XAG"] == 77
    assert payload["expires_at"] - payload["fetched_at"] == 300


def test_resolve_symbol_uses_disk_cache_across_processes(monkeypatch):
    async def first_fetch(_api_client):
        return {"perp": {"XAG": 77}, "spot": {}}

    monkeypatch.setattr(_symbols, "_fetch_symbols", first_fetch)
    first = _run(
        _symbols.resolve_symbol("XAG", "https://testnet.zklighter.elliot.ai", object())
    )
    assert first == (77, "perp", "XAG")

    _symbols._LIVE_CACHE.clear()

    async def should_not_run(_api_client):
        raise AssertionError("disk cache should satisfy second lookup")

    monkeypatch.setattr(_symbols, "_fetch_symbols", should_not_run)
    second = _run(
        _symbols.resolve_symbol("XAG", "https://testnet.zklighter.elliot.ai", object())
    )
    assert second == (77, "perp", "XAG")


def test_expired_cache_refreshes(monkeypatch):
    host = "https://staging.zklighter.elliot.ai"
    cache_path = symbol_cache_path(host)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "host": host,
                "fetched_at": 100,
                "expires_at": 200,
                "symbols": {"perp": {"XAG": 70}, "spot": {}},
            }
        ),
        encoding="utf-8",
    )

    async def fake_fetch(_api_client):
        return {"perp": {"XAG": 88}, "spot": {}}

    monkeypatch.setattr(_symbols, "_fetch_symbols", fake_fetch)
    monkeypatch.setattr(_symbols.time, "time", lambda: 500)

    resolved = _run(_symbols.resolve_symbol("XAG", host, object()))

    assert resolved == (88, "perp", "XAG")
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert payload["symbols"]["perp"]["XAG"] == 88
    assert payload["fetched_at"] == 500
    assert payload["expires_at"] == 800


def test_symbol_miss_refreshes_fresh_but_incomplete_cache(monkeypatch):
    host = "https://mainnet.zklighter.elliot.ai"
    cache_path = symbol_cache_path(host)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "host": host,
                "fetched_at": 500,
                "expires_at": 800,
                "symbols": {"perp": {"ETH": 0}, "spot": {}},
            }
        ),
        encoding="utf-8",
    )

    calls = {"count": 0}

    async def fake_fetch(_api_client):
        calls["count"] += 1
        return {"perp": {"ETH": 0, "LIT": 120}, "spot": {"LIT/USDC": 2049}}

    monkeypatch.setattr(_symbols, "_fetch_symbols", fake_fetch)
    monkeypatch.setattr(_symbols.time, "time", lambda: 600)

    resolved = _run(_symbols.resolve_symbol("LIT", host, object()))

    assert resolved == (120, "perp", "LIT")
    assert calls["count"] == 1

    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert payload["symbols"]["perp"]["LIT"] == 120
    assert payload["symbols"]["spot"]["LIT/USDC"] == 2049
