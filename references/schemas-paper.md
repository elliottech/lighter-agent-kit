# Paper Trading Response Schemas

All paper commands return JSON to stdout. Errors are `{"error": "..."}` with exit code 1.

## Refresh-warning envelope

`status`, `positions`, `health`, and `liquidation_price` auto-refresh mark prices for each open position before returning. If one or more of those per-market refreshes fails (e.g. transient network error), the command still succeeds from cached state and appends a `warnings.refresh_failed` object. Shape:

```json
{
  "... normal response fields ...": "...",
  "warnings": {
    "refresh_failed": {
      "BTC": "ClientConnectorError: Cannot connect to host ..."
    }
  }
}
```

The map is keyed by symbol; the value is `"<ExceptionClass>: <message>"`. The field is omitted on the happy path. Markets that refreshed successfully carry live marks; markets listed under `refresh_failed` carry the last-fill (or last-successful-refresh) mark. Pass `--no-refresh` to skip refresh entirely — then the field is never included.

---

## `init` / `reset`

```json
{
  "status": "ok",
  "collateral": 10000,
  "tier": "premium",
  "taker_fee_bps": 2.8,
  "maker_fee_bps": 0.4,
  "state_path": "/Users/you/.lighter/lighter-agent-kit/paper-state.json"
}
```

`reset` with no flags reuses the previous `collateral` and `tier`.

## `set_tier`

```json
{
  "status": "ok",
  "tier": "premium_3",
  "taker_fee_bps": 2.52,
  "maker_fee_bps": 0.36
}
```

## `status`

```json
{
  "status": "ok",
  "collateral": 9950.12,
  "initial_collateral": 10000,
  "tier": "premium",
  "taker_fee_bps": 2.8,
  "maker_fee_bps": 0.4,
  "unrealized_pnl": 25.0,
  "total_pnl": -24.88,
  "positions_count": 1,
  "trades_count": 2,
  "state_path": "/Users/you/.lighter/lighter-agent-kit/paper-state.json"
}
```

`total_pnl` = `(collateral - initial_collateral) + unrealized_pnl`.

> `status` auto-refreshes mark prices for all open positions before returning. Pass `--no-refresh` to use cached values (faster, but `unrealized_pnl` reflects the last fill or last refresh).

## `positions`

```json
{
  "positions": [
    {
      "symbol": "ETH",
      "market_id": 0,
      "side": "long",
      "size": 0.5,
      "avg_entry_price": 3500.0,
      "mark_price": 3550.0,
      "unrealized_pnl": 25.0,
      "realized_pnl": 10.0,
      "liquidation_price": 2800.0
    }
  ]
}
```

`--symbol ETH` filters. No `--symbol` → all open positions. Empty list when no positions are open.

> `positions` auto-refreshes mark prices for all open positions before returning (parallel HTTP fetch, ~300ms). Pass `--no-refresh` to use cached values.

## `trades`

```json
{
  "trades": [
    {
      "symbol": "ETH",
      "market_id": 0,
      "side": "buy",
      "size": 0.5,
      "price": 3500.0,
      "fee": 0.49,
      "realized_pnl": 0.0,
      "is_liquidation": false,
      "timestamp": "2024-06-15T12:00:00+00:00"
    }
  ]
}
```

Most recent first. `--limit 50` (default). `--symbol` filters.

## `health`

```json
{
  "status": "healthy",
  "total_account_value": 10025.0,
  "initial_margin_requirement": 88.75,
  "maintenance_margin_requirement": 53.25,
  "margin_usage": 0.89,
  "leverage": 0.177,
  "collateral": 10000.0,
  "tier": "premium",
  "taker_fee_bps": 2.8,
  "maker_fee_bps": 0.4
}
```

`status` is one of: `healthy`, `pre_liquidation`, `partial_liquidation`, `full_liquidation`, `bankruptcy`.

`margin_usage` is `initial_margin_requirement / total_account_value * 100` (percentage). `leverage` is `total_notional / total_account_value`.

> `health` and `liquidation_price` auto-refresh mark prices for all open positions before computing. Pass `--no-refresh` to compute from cached marks.

## `liquidation_price`

With an open position:

```json
{
  "symbol": "ETH",
  "market_id": 0,
  "liquidation_price": 2800.0,
  "mark_price": 3550.0,
  "position_side": "long",
  "position_size": 0.5
}
```

No open position:

```json
{
  "symbol": "ETH",
  "market_id": 0,
  "liquidation_price": 0,
  "note": "no open position"
}
```

`liquidation_price` = 0 also means the position is so overcollateralized that liquidation is not reachable at any positive price.

## `refresh`

```json
{
  "status": "ok",
  "symbol": "ETH",
  "market_id": 0,
  "mid_price": 3499.5,
  "best_bid": "3499.00",
  "best_ask": "3500.00"
}
```

## `order market`

```json
{
  "status": "ok",
  "symbol": "ETH",
  "market_id": 0,
  "side": "long",
  "order_type": "market",
  "filled_size": 0.1,
  "avg_price": 3500.0,
  "total_fee": 0.098,
  "quote_amount": 350.0,
  "unfilled": 0.0,
  "liquidated": false,
  "fills_count": 1
}
```

## `order ioc`

```json
{
  "status": "ok",
  "symbol": "ETH",
  "market_id": 0,
  "side": "long",
  "order_type": "ioc",
  "limit_price": 3510.0,
  "filled_size": 0.1,
  "avg_price": 3500.0,
  "total_fee": 0.098,
  "quote_amount": 350.0,
  "unfilled": 0.0,
  "liquidated": false,
  "fills_count": 1
}
```

`unfilled` > 0 means the book didn't have enough depth within the limit price.

## Common error shapes

```json
{"error": "no paper account; run `paper.py init` first"}
{"error": "--amount must be positive"}
{"error": "unknown symbol 'XYZ'; place an order or use `paper.py refresh --symbol XYZ` to discover it"}
{"error": "paper trading only supports perp markets (market_id < 2048), got 2048"}
{"error": "paper account already exists; use `paper.py reset` to reinitialize"}
```

## Available tiers

Runtime tier truth comes from vendored SDK enum `lighter.paper_client.AccountTier`.

| Tier | Taker bps | Maker bps |
|------|-----------|-----------|
| `standard` | 0.0 | 0.0 |
| `premium` | 2.8 | 0.4 |
| `premium_1` | 2.73 | 0.39 |
| `premium_2` | 2.66 | 0.38 |
| `premium_3` | 2.52 | 0.36 |
| `premium_4` | 2.38 | 0.34 |
| `premium_5` | 2.24 | 0.32 |
| `premium_6` | 2.1 | 0.3 |
| `premium_7` | 1.96 | 0.28 |
