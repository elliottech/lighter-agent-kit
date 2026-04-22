"""Offline tests for paper.py — no network calls.

Run: pip install pytest && pytest tests/ -v
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Bootstrap vendored SDK so `import paper` succeeds
# ---------------------------------------------------------------------------

_TEST_DIR = os.path.dirname(os.path.abspath(__file__))
_SKILL_DIR = os.path.dirname(_TEST_DIR)
_SCRIPTS_DIR = os.path.join(_SKILL_DIR, "scripts")
sys.path.insert(0, _SCRIPTS_DIR)

from _sdk import ensure_lighter  # noqa: E402

ensure_lighter()

import paper  # noqa: E402
from lighter.paper_client import (  # noqa: E402
    AccountTier,
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
from lighter.paper_client.risk import (  # noqa: E402
    compute_health,
    compute_liquidation_price,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """Point paper state to a temp dir so tests never collide."""
    state_file = tmp_path / "paper-state.json"
    monkeypatch.setenv("LIGHTER_PAPER_STATE_PATH", str(state_file))


@pytest.fixture
def sample_account():
    acct = new_paper_account(10_000.0)
    acct.positions[0] = PaperPosition(
        market_id=0,
        size=0.5,
        entry_quote=1750.0,
        avg_entry_price=3500.0,
        mark_price=3550.0,
        unrealized_pnl=25.0,
        realized_pnl=10.0,
        liquidation_price=2800.0,
    )
    acct.trades.append(
        PaperTrade(
            market_id=0,
            side=PaperOrderSide.BUY,
            size=0.5,
            price=3500.0,
            fee=0.49,
            realized_pnl=0.0,
            is_liquidation=False,
            timestamp=datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc),
        )
    )
    return acct


@pytest.fixture
def sample_config():
    return MarketConfig(
        market_id=0,
        symbol="ETH",
        size_decimals=4,
        price_decimals=2,
        default_initial_margin_fraction=500,
        min_initial_margin_fraction=100,
        maintenance_margin_fraction=300,
        closeout_margin_fraction=200,
        taker_fee=0.00028,
        maker_fee=0.00004,
        min_base_amount=0.001,
        min_quote_amount=1.0,
        last_trade_price=3500.0,
    )


def _mock_order_book_detail(
    market_id=0, symbol="ETH", market_type="perp",
):
    detail = MagicMock()
    detail.market_id = market_id
    detail.symbol = symbol
    detail.market_type = market_type
    detail.size_decimals = 4
    detail.price_decimals = 2
    detail.default_initial_margin_fraction = 500
    detail.min_initial_margin_fraction = 100
    detail.maintenance_margin_fraction = 300
    detail.closeout_margin_fraction = 200
    detail.min_base_amount = "0.001"
    detail.min_quote_amount = "1.0"
    detail.last_trade_price = "3500.0"
    return detail


def _mock_order_book_snapshot():
    """Dict-based order book that satisfies InMemoryOrderBook Mapping path."""
    return {
        "asks": [
            {"price": "3500.00", "size": "1.0"},
            {"price": "3501.00", "size": "2.0"},
            {"price": "3502.00", "size": "3.0"},
        ],
        "bids": [
            {"price": "3499.00", "size": "1.0"},
            {"price": "3498.00", "size": "2.0"},
            {"price": "3497.00", "size": "3.0"},
        ],
    }


@pytest.fixture
def mock_api():
    """Patch lighter API layer for commands that hit the network."""
    mock_ob = _mock_order_book_snapshot()
    mock_detail = _mock_order_book_detail()
    mock_details_resp = MagicMock()
    mock_details_resp.order_book_details = [mock_detail]

    mock_book_entry = MagicMock()
    mock_book_entry.market_id = 0
    mock_book_entry.symbol = "ETH"
    mock_book_entry.market_type = "perp"
    mock_books_resp = MagicMock()
    mock_books_resp.order_books = [mock_book_entry]

    mock_order_api = AsyncMock()
    mock_order_api.order_book_orders = AsyncMock(return_value=mock_ob)
    mock_order_api.order_book_details = AsyncMock(
        return_value=mock_details_resp,
    )
    mock_order_api.order_books = AsyncMock(return_value=mock_books_resp)

    mock_api_cm = AsyncMock()
    mock_api_cm.configuration = MagicMock()
    mock_api_cm.configuration.host = "https://testnet.zklighter.elliot.ai"
    mock_api_cm.__aenter__ = AsyncMock(return_value=mock_api_cm)
    mock_api_cm.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("lighter.ApiClient", return_value=mock_api_cm),
        patch("lighter.OrderApi", return_value=mock_order_api),
        patch(
            "lighter.paper_client.client.OrderApi",
            return_value=mock_order_api,
        ),
    ):
        yield mock_order_api


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def _parse_output(capsys):
    return json.loads(capsys.readouterr().out)


# =========================================================================
# 1. State serializer round-trip
# =========================================================================


class TestStateSerialization:
    def test_tier_map_is_derived_from_account_tier(self):
        expected = {tier.name.lower(): tier for tier in AccountTier}
        assert paper.TIER_MAP == expected
        assert paper.TIER_CHOICES == tuple(expected.keys())

    def test_position_roundtrip(self):
        pos = PaperPosition(
            market_id=0,
            size=-1.5,
            entry_quote=5250.0,
            avg_entry_price=3500.0,
            mark_price=3400.0,
            unrealized_pnl=150.0,
            realized_pnl=42.0,
            liquidation_price=4200.0,
        )
        restored = paper._deser_position(paper._ser_position(pos))
        assert restored.market_id == pos.market_id
        assert restored.size == pos.size
        assert restored.entry_quote == pos.entry_quote
        assert restored.avg_entry_price == pos.avg_entry_price
        assert restored.mark_price == pos.mark_price
        assert restored.unrealized_pnl == pos.unrealized_pnl
        assert restored.realized_pnl == pos.realized_pnl
        assert restored.liquidation_price == pos.liquidation_price

    def test_trade_roundtrip(self):
        ts = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        trade = PaperTrade(
            market_id=0,
            side=PaperOrderSide.SELL,
            size=0.5,
            price=3500.0,
            fee=0.49,
            realized_pnl=12.5,
            is_liquidation=True,
            timestamp=ts,
        )
        restored = paper._deser_trade(paper._ser_trade(trade))
        assert restored.market_id == trade.market_id
        assert restored.side == trade.side
        assert restored.size == trade.size
        assert restored.price == trade.price
        assert restored.fee == trade.fee
        assert restored.realized_pnl == trade.realized_pnl
        assert restored.is_liquidation == trade.is_liquidation
        assert restored.timestamp == trade.timestamp

    def test_account_roundtrip(self, sample_account):
        restored = paper._deser_account(
            paper._ser_account(sample_account),
        )
        assert restored.initial_collateral == sample_account.initial_collateral
        assert restored.collateral == sample_account.collateral
        assert len(restored.positions) == len(sample_account.positions)
        assert len(restored.trades) == len(sample_account.trades)
        assert restored.positions[0].size == 0.5
        assert restored.positions[0].avg_entry_price == 3500.0
        assert restored.trades[0].side == PaperOrderSide.BUY

    def test_market_config_roundtrip(self, sample_config):
        restored = paper._deser_config(paper._ser_config(sample_config))
        assert restored.market_id == sample_config.market_id
        assert restored.symbol == sample_config.symbol
        assert restored.taker_fee == sample_config.taker_fee
        assert restored.maker_fee == sample_config.maker_fee
        assert restored.size_decimals == sample_config.size_decimals

    def test_full_state_roundtrip(self, sample_account, sample_config):
        paper._save_state("premium", sample_account, {0: sample_config})
        state = paper._load_state()
        assert state is not None
        assert state["version"] == paper.STATE_VERSION
        assert state["tier"] == "premium"

        tier_name, tier_enum, acct, configs = paper._unpack_state(state)
        assert tier_name == "premium"
        assert tier_enum == AccountTier.PREMIUM
        assert acct.collateral == sample_account.collateral
        assert 0 in configs
        assert configs[0].symbol == "ETH"

    def test_load_state_missing_file(self):
        assert paper._load_state() is None

    def test_load_state_corrupt_file_errors(self, capsys):
        path = paper._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ this is not valid json", encoding="utf-8")
        with pytest.raises(SystemExit):
            paper._load_state()
        result = _parse_output(capsys)
        assert "corrupted" in result["error"]
        assert "reset" in result["error"]

    def test_load_state_wrong_version_errors(self, capsys):
        path = paper._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"version": 99, "tier": "premium", "account": {}}),
            encoding="utf-8",
        )
        with pytest.raises(SystemExit):
            paper._load_state()
        result = _parse_output(capsys)
        assert "version mismatch" in result["error"]
        assert "reset" in result["error"]

    def test_try_load_state_swallows_corruption(self):
        """reset must still work on a corrupted state file."""
        path = paper._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ garbage", encoding="utf-8")
        assert paper._try_load_state() is None

    def test_try_load_state_swallows_wrong_version(self):
        path = paper._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"version": 99}), encoding="utf-8",
        )
        assert paper._try_load_state() is None

    def test_try_load_state_swallows_invalid_schema(self):
        path = paper._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"version": 1}), encoding="utf-8",
        )
        assert paper._try_load_state() is None

    def test_reset_recovers_from_corruption(self, capsys):
        """End-to-end: corrupt state → reset works, starts fresh."""
        path = paper._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json", encoding="utf-8")

        _run(paper.cmd_reset(
            SimpleNamespace(command="reset", collateral=None, tier=None),
        ))
        result = _parse_output(capsys)
        assert result["status"] == "ok"
        # Falls back to defaults since previous state was unreadable
        assert result["collateral"] == 10_000
        assert result["tier"] == "premium"

    def test_reset_recovers_from_invalid_schema(self, capsys):
        path = paper._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"version": 1}), encoding="utf-8",
        )

        _run(paper.cmd_reset(
            SimpleNamespace(command="reset", collateral=None, tier=None),
        ))
        result = _parse_output(capsys)
        assert result["status"] == "ok"
        assert result["collateral"] == 10_000
        assert result["tier"] == "premium"

    def test_status_on_corrupt_state_surfaces_error(self, capsys):
        """Regression: corruption must not look like "no account"."""
        path = paper._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ bad", encoding="utf-8")

        with pytest.raises(SystemExit):
            _run(paper.cmd_status(SimpleNamespace(command="status")))
        result = _parse_output(capsys)
        # Must be the corruption message, not the "no account" one
        assert "corrupted" in result["error"]
        assert "no paper account" not in result["error"]

    def test_status_on_invalid_schema_surfaces_corruption(self, capsys):
        path = paper._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"version": 1}), encoding="utf-8",
        )

        with pytest.raises(SystemExit):
            _run(paper.cmd_status(SimpleNamespace(command="status")))
        result = _parse_output(capsys)
        assert "corrupted" in result["error"]
        assert "tier" in result["error"]

    def test_unknown_tier_errors_instead_of_falling_back(self, capsys):
        path = paper._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({
                "version": 1,
                "tier": "premium_8",
                "account": paper._ser_account(new_paper_account(10_000)),
                "market_configs": {},
            }),
            encoding="utf-8",
        )

        with pytest.raises(SystemExit):
            _run(paper.cmd_status(SimpleNamespace(command="status")))
        result = _parse_output(capsys)
        assert "invalid tier" in result["error"]
        assert "premium_8" in result["error"]


# =========================================================================
# 2. Init / Reset flows
# =========================================================================


class TestInitReset:
    def test_init_default_args(self, capsys):
        _run(paper.cmd_init(
            SimpleNamespace(command="init", collateral=10_000, tier="premium"),
        ))
        result = _parse_output(capsys)
        assert result["status"] == "ok"
        assert result["collateral"] == 10_000
        assert result["tier"] == "premium"
        assert result["taker_fee_bps"] == 2.8
        assert result["maker_fee_bps"] == 0.4
        assert "state_path" in result

    def test_init_custom_args(self, capsys):
        _run(paper.cmd_init(
            SimpleNamespace(command="init", collateral=5000, tier="standard"),
        ))
        result = _parse_output(capsys)
        assert result["collateral"] == 5000
        assert result["tier"] == "standard"
        assert result["taker_fee_bps"] == 0.0

    def test_init_rejects_duplicate(self, capsys):
        _run(paper.cmd_init(
            SimpleNamespace(command="init", collateral=10_000, tier="premium"),
        ))
        capsys.readouterr()
        with pytest.raises(SystemExit):
            _run(paper.cmd_init(
                SimpleNamespace(
                    command="init", collateral=10_000, tier="premium",
                ),
            ))
        result = _parse_output(capsys)
        assert "already exists" in result["error"]

    def test_reset_reuses_previous(self, capsys):
        _run(paper.cmd_init(
            SimpleNamespace(command="init", collateral=7500, tier="premium_3"),
        ))
        capsys.readouterr()
        _run(paper.cmd_reset(
            SimpleNamespace(command="reset", collateral=None, tier=None),
        ))
        result = _parse_output(capsys)
        assert result["collateral"] == 7500
        assert result["tier"] == "premium_3"

    def test_reset_with_overrides(self, capsys):
        _run(paper.cmd_init(
            SimpleNamespace(command="init", collateral=10_000, tier="premium"),
        ))
        capsys.readouterr()
        _run(paper.cmd_reset(
            SimpleNamespace(
                command="reset", collateral=20_000, tier="standard",
            ),
        ))
        result = _parse_output(capsys)
        assert result["collateral"] == 20_000
        assert result["tier"] == "standard"

    def test_reset_without_prior_init(self, capsys):
        _run(paper.cmd_reset(
            SimpleNamespace(command="reset", collateral=None, tier=None),
        ))
        result = _parse_output(capsys)
        assert result["collateral"] == 10_000
        assert result["tier"] == "premium"

    def test_set_tier(self, capsys, sample_account, sample_config):
        paper._save_state("premium", sample_account, {0: sample_config})
        _run(paper.cmd_set_tier(
            SimpleNamespace(command="set_tier", tier="premium_3"),
        ))
        result = _parse_output(capsys)
        assert result["status"] == "ok"
        assert result["tier"] == "premium_3"
        # Verify fees were updated on cached config
        state = paper._load_state()
        _, _, _, configs = paper._unpack_state(state)
        assert configs[0].taker_fee == AccountTier.PREMIUM_3.taker_fee
        assert configs[0].maker_fee == AccountTier.PREMIUM_3.maker_fee


# =========================================================================
# 3. Read commands (status, positions, trades)
# =========================================================================


class TestReadCommands:
    def test_status(self, capsys, sample_account, sample_config):
        paper._save_state("premium", sample_account, {0: sample_config})
        _run(paper.cmd_status(
            SimpleNamespace(command="status", no_refresh=True),
        ))
        result = _parse_output(capsys)
        assert result["status"] == "ok"
        assert result["collateral"] == sample_account.collateral
        assert result["initial_collateral"] == sample_account.initial_collateral
        assert result["tier"] == "premium"
        assert result["positions_count"] == 1
        assert result["trades_count"] == 1
        assert "warnings" not in result  # no refresh attempted → no warnings

    def test_positions_all(self, capsys, sample_account, sample_config):
        paper._save_state("premium", sample_account, {0: sample_config})
        _run(paper.cmd_positions(
            SimpleNamespace(command="positions", symbol=None, no_refresh=True),
        ))
        result = _parse_output(capsys)
        assert len(result["positions"]) == 1
        pos = result["positions"][0]
        assert pos["symbol"] == "ETH"
        assert pos["side"] == "long"
        assert pos["size"] == 0.5
        assert pos["avg_entry_price"] == 3500.0

    def test_positions_filter_by_symbol(
        self, capsys, sample_account, sample_config,
    ):
        paper._save_state("premium", sample_account, {0: sample_config})
        _run(paper.cmd_positions(
            SimpleNamespace(command="positions", symbol="BTC", no_refresh=True),
        ))
        result = _parse_output(capsys)
        assert len(result["positions"]) == 0

    def test_trades(self, capsys, sample_account, sample_config):
        paper._save_state("premium", sample_account, {0: sample_config})
        _run(paper.cmd_trades(
            SimpleNamespace(command="trades", symbol=None, limit=50),
        ))
        result = _parse_output(capsys)
        assert len(result["trades"]) == 1
        t = result["trades"][0]
        assert t["symbol"] == "ETH"
        assert t["side"] == "buy"
        assert t["size"] == 0.5

    def test_trades_limited(self, capsys, sample_config):
        acct = new_paper_account(10_000)
        ts = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        for i in range(10):
            acct.trades.append(
                PaperTrade(
                    market_id=0,
                    side=PaperOrderSide.BUY,
                    size=0.1,
                    price=3500.0 + i,
                    fee=0.01,
                    realized_pnl=0.0,
                    is_liquidation=False,
                    timestamp=ts,
                )
            )
        paper._save_state("premium", acct, {0: sample_config})
        _run(paper.cmd_trades(
            SimpleNamespace(command="trades", symbol=None, limit=3),
        ))
        result = _parse_output(capsys)
        assert len(result["trades"]) == 3
        # Most recent first
        assert result["trades"][0]["price"] == 3509.0

    def test_require_state_errors_when_missing(self, capsys):
        with pytest.raises(SystemExit):
            paper._require_state()
        result = _parse_output(capsys)
        assert "no paper account" in result["error"]

    def test_status_refresh_happy_path(
        self, capsys, sample_account, sample_config, mock_api,
    ):
        """With refresh enabled and a working API, no warnings appear."""
        paper._save_state("premium", sample_account, {0: sample_config})
        _run(paper.cmd_status(
            SimpleNamespace(command="status", no_refresh=False),
        ))
        result = _parse_output(capsys)
        assert result["status"] == "ok"
        assert "warnings" not in result
        # order_book_orders was called for the open position
        assert mock_api.order_book_orders.await_count >= 1

    def test_status_refresh_failure_falls_back_with_warning(
        self, capsys, sample_account, sample_config,
    ):
        """Refresh failure must not kill the read — cached state serves,
        with a `warnings.refresh_failed` field flagging the stale market."""
        paper._save_state("premium", sample_account, {0: sample_config})

        mock_api_cm = AsyncMock()
        mock_api_cm.configuration = MagicMock()
        mock_api_cm.configuration.host = "https://testnet.zklighter.elliot.ai"
        mock_api_cm.__aenter__ = AsyncMock(return_value=mock_api_cm)
        mock_api_cm.__aexit__ = AsyncMock(return_value=False)

        async def _boom(self, market_id):
            raise ConnectionError("simulated network failure")

        with (
            patch("lighter.ApiClient", return_value=mock_api_cm),
            patch.object(PaperClient, "track_market_snapshot", _boom),
        ):
            _run(paper.cmd_status(
                SimpleNamespace(command="status", no_refresh=False),
            ))

        result = _parse_output(capsys)
        assert result["status"] == "ok"
        # Cached values still surface
        assert result["collateral"] == sample_account.collateral
        # And the failure is visible
        assert "warnings" in result
        failed = result["warnings"]["refresh_failed"]
        assert "ETH" in failed
        assert "ConnectionError" in failed["ETH"]


# =========================================================================
# 4. Health / Liquidation math
# =========================================================================


class TestHealthAndRisk:
    def test_health_no_positions(self, capsys):
        _run(paper.cmd_init(
            SimpleNamespace(command="init", collateral=10_000, tier="premium"),
        ))
        capsys.readouterr()
        _run(paper.cmd_health(
            SimpleNamespace(command="health", no_refresh=True),
        ))
        result = _parse_output(capsys)
        assert result["status"] == "healthy"
        assert result["total_account_value"] == 10_000
        assert result["leverage"] == 0
        assert result["margin_usage"] == 0

    def test_health_with_position(
        self, capsys, sample_account, sample_config,
    ):
        paper._save_state("premium", sample_account, {0: sample_config})
        _run(paper.cmd_health(
            SimpleNamespace(command="health", no_refresh=True),
        ))
        result = _parse_output(capsys)
        assert result["status"] == "healthy"
        assert result["total_account_value"] > 0
        assert result["leverage"] > 0
        assert result["initial_margin_requirement"] > 0

    def test_liquidation_price_with_position(
        self, capsys, sample_config,
    ):
        # Use a leveraged account so liq price is computable (not 0)
        acct = PaperAccount(
            initial_collateral=500.0,
            collateral=500.0,
            positions={
                0: PaperPosition(
                    market_id=0,
                    size=0.5,
                    entry_quote=1750.0,
                    avg_entry_price=3500.0,
                    mark_price=3550.0,
                    unrealized_pnl=25.0,
                    realized_pnl=0.0,
                    liquidation_price=0.0,
                ),
            },
        )
        paper._save_state("premium", acct, {0: sample_config})
        _run(paper.cmd_liquidation_price(SimpleNamespace(
            command="liquidation_price", symbol="ETH", no_refresh=True,
        )))
        result = _parse_output(capsys)
        assert result["symbol"] == "ETH"
        assert result["liquidation_price"] > 0
        assert result["position_side"] == "long"
        assert result["position_size"] == 0.5

    def test_liquidation_price_no_position(
        self, capsys, sample_config,
    ):
        acct = new_paper_account(10_000)
        paper._save_state("premium", acct, {0: sample_config})
        _run(paper.cmd_liquidation_price(SimpleNamespace(
            command="liquidation_price", symbol="ETH", no_refresh=True,
        )))
        result = _parse_output(capsys)
        assert result["liquidation_price"] == 0
        assert result["note"] == "no open position"

    def test_health_computation_directly(self, sample_account, sample_config):
        """Verify compute_health with a known position."""
        mark_prices = {0: 3550.0}
        configs = {0: sample_config}
        health = compute_health(sample_account, mark_prices, configs)
        assert health.total_account_value > 0
        assert health.leverage > 0
        # TAV = collateral + unrealized_pnl
        # unrealized for long: abs(0.5) * 3550 - 1750 = 1775 - 1750 = 25
        expected_tav = sample_account.collateral + 25.0
        assert abs(health.total_account_value - expected_tav) < 0.01

    def test_liquidation_price_computation_directly(self, sample_config):
        # Leveraged account: 500 USDC collateral, 0.5 ETH long
        acct = PaperAccount(
            initial_collateral=500.0,
            collateral=500.0,
            positions={
                0: PaperPosition(
                    market_id=0, size=0.5, entry_quote=1750.0,
                    avg_entry_price=3500.0,
                ),
            },
        )
        mark_prices = {0: 3550.0}
        configs = {0: sample_config}
        liq = compute_liquidation_price(acct, 0, mark_prices, configs)
        assert liq > 0
        # Liq price for a long should be below the current mark
        assert liq < 3550.0


# =========================================================================
# 5. Spot market rejection
# =========================================================================


class TestSpotRejection:
    def test_perp_validation_rejects_spot_2048(self):
        with pytest.raises(ValueError, match="perp"):
            PaperClient._validate_perp_market_id(2048)

    def test_perp_validation_rejects_spot_4094(self):
        with pytest.raises(ValueError, match="perp"):
            PaperClient._validate_perp_market_id(4094)

    def test_perp_validation_accepts_perp_0(self):
        PaperClient._validate_perp_market_id(0)  # should not raise

    def test_perp_validation_accepts_perp_2047(self):
        PaperClient._validate_perp_market_id(2047)  # should not raise


# =========================================================================
# 6. Order placement (PaperClient with mocked OrderApi)
# =========================================================================


class TestOrderPlacement:
    @pytest.fixture
    def paper_client(self):
        """Create a PaperClient with a mocked OrderApi (no network)."""
        mock_detail = _mock_order_book_detail()
        mock_details_resp = MagicMock()
        mock_details_resp.order_book_details = [mock_detail]

        mock_order_api = AsyncMock()
        mock_order_api.order_book_orders = AsyncMock(
            return_value=_mock_order_book_snapshot(),
        )
        mock_order_api.order_book_details = AsyncMock(
            return_value=mock_details_resp,
        )

        client = PaperClient(
            api_client=None,
            initial_collateral_usdc=10_000,
            order_api=mock_order_api,
            account_tier=AccountTier.PREMIUM,
            ws_url="ws://localhost/stream",
        )
        return client

    def test_market_buy(self, paper_client):
        async def _go():
            await paper_client.track_market_snapshot(0)
            result = await paper_client.create_paper_order(
                PaperOrderRequest(
                    market_id=0,
                    side=PaperOrderSide.BUY,
                    base_amount=0.1,
                    order_type=PaperOrderType.MARKET,
                ),
            )
            assert result.filled_size == pytest.approx(0.1)
            # Best ask is 3500, so avg_price should be 3500
            assert result.avg_price == pytest.approx(3500.0)
            assert result.total_fee > 0
            assert result.unfilled == pytest.approx(0.0)
            # Verify position was created
            pos = paper_client.get_position(0)
            assert pos is not None
            assert pos.size == pytest.approx(0.1)

        _run(_go())

    def test_market_sell(self, paper_client):
        async def _go():
            await paper_client.track_market_snapshot(0)
            result = await paper_client.create_paper_order(
                PaperOrderRequest(
                    market_id=0,
                    side=PaperOrderSide.SELL,
                    base_amount=0.1,
                    order_type=PaperOrderType.MARKET,
                ),
            )
            assert result.filled_size == pytest.approx(0.1)
            # Best bid is 3499
            assert result.avg_price == pytest.approx(3499.0)
            pos = paper_client.get_position(0)
            assert pos is not None
            assert pos.size == pytest.approx(-0.1)

        _run(_go())

    def test_ioc_buy(self, paper_client):
        async def _go():
            await paper_client.track_market_snapshot(0)
            result = await paper_client.create_paper_order(
                PaperOrderRequest(
                    market_id=0,
                    side=PaperOrderSide.BUY,
                    base_amount=0.5,
                    price=3501.0,
                    order_type=PaperOrderType.IOC,
                ),
            )
            # Should fill at 3500 (1.0) and 3501 (partial to reach 0.5)
            assert result.filled_size == pytest.approx(0.5)
            assert result.avg_price > 0
            assert result.total_fee > 0

        _run(_go())

    def test_ioc_buy_partial_fill(self, paper_client):
        async def _go():
            await paper_client.track_market_snapshot(0)
            # Price limit at exactly best ask — only fills the first level
            result = await paper_client.create_paper_order(
                PaperOrderRequest(
                    market_id=0,
                    side=PaperOrderSide.BUY,
                    base_amount=5.0,
                    price=3500.0,
                    order_type=PaperOrderType.IOC,
                ),
            )
            # Only best ask (1.0 @ 3500) fills; rest unfilled
            assert result.filled_size == pytest.approx(1.0)
            assert result.unfilled == pytest.approx(4.0)

        _run(_go())

    def test_fee_applied_to_collateral(self, paper_client):
        async def _go():
            await paper_client.track_market_snapshot(0)
            initial = paper_client.get_collateral()
            result = await paper_client.create_paper_order(
                PaperOrderRequest(
                    market_id=0,
                    side=PaperOrderSide.BUY,
                    base_amount=0.1,
                    order_type=PaperOrderType.MARKET,
                ),
            )
            # Collateral should decrease by fee
            assert paper_client.get_collateral() == pytest.approx(
                initial - result.total_fee,
            )

        _run(_go())

    def test_open_and_close_position(self, paper_client):
        async def _go():
            await paper_client.track_market_snapshot(0)
            # Open long
            await paper_client.create_paper_order(
                PaperOrderRequest(
                    market_id=0,
                    side=PaperOrderSide.BUY,
                    base_amount=0.1,
                    order_type=PaperOrderType.MARKET,
                ),
            )
            assert paper_client.get_position(0) is not None
            # Close long
            await paper_client.create_paper_order(
                PaperOrderRequest(
                    market_id=0,
                    side=PaperOrderSide.SELL,
                    base_amount=0.1,
                    order_type=PaperOrderType.MARKET,
                ),
            )
            # Position should be closed (removed)
            assert paper_client.get_position(0) is None
            # Two trades recorded
            assert len(paper_client.get_trades()) == 2

        _run(_go())


# =========================================================================
# 7. End-to-end CLI with mocked API
# =========================================================================


class TestCLIEndToEnd:
    def test_place_market_via_cli(self, mock_api, capsys):
        _run(paper.cmd_init(
            SimpleNamespace(command="init", collateral=10_000, tier="premium"),
        ))
        capsys.readouterr()

        _run(paper.cmd_order_market(
            SimpleNamespace(
                command="place_market",
                symbol="ETH",
                side="buy",
                amount=0.1,
            ),
        ))
        result = _parse_output(capsys)
        assert result["status"] == "ok"
        assert result["symbol"] == "ETH"
        assert result["side"] == "long"
        assert result["filled_size"] > 0
        assert result["avg_price"] > 0
        assert result["total_fee"] > 0
        assert result["liquidated"] is False

    def test_place_ioc_via_cli(self, mock_api, capsys):
        _run(paper.cmd_init(
            SimpleNamespace(command="init", collateral=10_000, tier="premium"),
        ))
        capsys.readouterr()

        _run(paper.cmd_order_ioc(
            SimpleNamespace(
                command="place_ioc",
                symbol="ETH",
                side="buy",
                amount=0.1,
                price=3510.0,
            ),
        ))
        result = _parse_output(capsys)
        assert result["status"] == "ok"
        assert result["order_type"] == "ioc"
        assert result["filled_size"] > 0

    def test_place_market_persists_state(self, mock_api, capsys):
        _run(paper.cmd_init(
            SimpleNamespace(command="init", collateral=10_000, tier="premium"),
        ))
        capsys.readouterr()

        _run(paper.cmd_order_market(
            SimpleNamespace(
                command="place_market",
                symbol="ETH",
                side="buy",
                amount=0.1,
            ),
        ))
        capsys.readouterr()

        # Verify state was saved with position
        _run(paper.cmd_positions(
            SimpleNamespace(command="positions", symbol=None),
        ))
        result = _parse_output(capsys)
        assert len(result["positions"]) == 1
        assert result["positions"][0]["symbol"] == "ETH"

    def test_place_market_negative_amount_rejected(self, capsys):
        _run(paper.cmd_init(
            SimpleNamespace(command="init", collateral=10_000, tier="premium"),
        ))
        capsys.readouterr()

        with pytest.raises(SystemExit):
            _run(paper.cmd_order_market(
                SimpleNamespace(
                    command="place_market",
                    symbol="ETH",
                    side="buy",
                    amount=-1,
                ),
            ))
        result = _parse_output(capsys)
        assert "positive" in result["error"]

    def test_refresh_via_cli(self, mock_api, capsys):
        _run(paper.cmd_init(
            SimpleNamespace(command="init", collateral=10_000, tier="premium"),
        ))
        capsys.readouterr()

        _run(paper.cmd_refresh(
            SimpleNamespace(command="refresh", symbol="ETH"),
        ))
        result = _parse_output(capsys)
        assert result["status"] == "ok"
        assert result["symbol"] == "ETH"
        assert result["market_id"] == 0
        assert result["mid_price"] is not None

    def test_refresh_caches_market_config(self, mock_api, capsys):
        _run(paper.cmd_init(
            SimpleNamespace(command="init", collateral=10_000, tier="premium"),
        ))
        capsys.readouterr()

        _run(paper.cmd_refresh(
            SimpleNamespace(command="refresh", symbol="ETH"),
        ))
        capsys.readouterr()

        # Market config should now be in state
        state = paper._load_state()
        assert "0" in state["market_configs"]
        assert state["market_configs"]["0"]["symbol"] == "ETH"
