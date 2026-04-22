# Read Command Response Schemas (`query.py`)

Schemas for the JSON returned by the more complex read commands. Trivial commands (`system status`, `health`) are self-explanatory; everything below documents the fields most users care about.

All numeric strings (prices, amounts, balances) are decimal-formatted strings — Lighter never returns floats for monetary values, to avoid precision loss.

---

## `market info`

Lists every market on the exchange.

```json
{
  "code": 200,
  "order_books": [
    {
      "symbol": "ETH",
      "market_id": 0,
      "market_type": "perp",            // "perp" or "spot"
      "base_asset_id": 1,
      "quote_asset_id": 3,
      "status": "active",
      "taker_fee": "0.00045",
      "maker_fee": "0.00010",
      "liquidation_fee": "0.00500",
      "min_base_amount": "0.0001",
      "min_quote_amount": "10",
      "order_quote_limit": "1000000",
      "supported_size_decimals": 4,     // amount precision
      "supported_price_decimals": 2,    // price precision
      "supported_quote_decimals": 6
    }
  ]
}
```

`supported_size_decimals` and `supported_price_decimals` are what `trade.py` uses internally to scale human prices/amounts into integer ticks/lots. See [schemas-write.md](schemas-write.md#precision-echoback) for the `effective_amount` / `effective_price` echoback mechanism.

> **Field naming:** raw API responses use `market_id` (both here in `market info` and in `market trades`, `market funding`, etc.), but every `--market_index` CLI flag and positional `<symbol>` on `query.py` and `trade.py` refers to the same value. The compact `market list` projection re-keys it as `market_index` so the response matches the flag name; everywhere else you'll see it as `market_id` in the raw shape. They are the same integer.

---

## `market book`

Top-of-book bids and asks for one market.

```json
{
  "code": 200,
  "total_asks": 20,
  "asks": [
    {
      "order_index": 281474984786237,
      "order_id": "281474984786237",
      "owner_account_index": 281474976710654,
      "initial_base_amount": "88.9470",
      "remaining_base_amount": "88.9226",
      "price": "2244.08",
      "order_expiry": 1778205086998,
      "transaction_time": 0
    }
  ],
  "total_bids": 20,
  "bids": [/* same shape, sorted descending by price */]
}
```

Asks are sorted ascending (best first), bids descending (best first). Default `--limit` is 20 per side.

---

## `market trades`

```json
{
  "code": 200,
  "trades": [
    {
      "trade_id": 4577322,
      "market_id": 0,
      "size": "0.0001",
      "price": "2243.85",
      "usd_amount": "0.224385",
      "is_maker_ask": false,            // false = aggressive buy, true = aggressive sell
      "ask_account_id": 9,
      "bid_account_id": 281474976710654,
      "timestamp": 1775622620404,       // ms
      "block_height": 958685,
      "tx_hash": "..."
    }
  ]
}
```

---

## `market candles`

```json
{
  "code": 200,
  "r": "1h",                            // resolution
  "c": [
    {
      "t": 1776646800000,               // bucket start, ms
      "o": 2278.62,                     // open
      "h": 2293.85,                     // high
      "l": 2276.55,                     // low
      "c": 2284.93,                     // close
      "v": 13655.42,                    // base volume (e.g. ETH)
      "V": 31253016.06,                 // quote volume (e.g. USDC)
      "i": 18206717124                  // last trade id in bucket
    }
  ]
}
```

---

## `market funding`

```json
{
  "code": 200,
  "funding_rates": [
    {
      "market_id": 0,
      "exchange": "binance",
      "symbol": "ETH",
      "rate": 0.0001                    // 8h-equivalent funding rate
    }
  ]
}
```

`rate` is the API's normalized funding value, not necessarily the venue's raw funding interval. The `/api/v1/funding-rates` endpoint returns an **8-hour-equivalent** rate across exchanges so values can be compared side by side, even when the underlying venue may update funding hourly.

---

## `account info`

```json
{
  "code": 200,
  "total": 1,
  "accounts": [
    {
      "index": 41,
      "l1_address": "0x...",
      "account_type": 0,
      "status": 0,
      "collateral": "0.000000",
      "available_balance": "0.000000",
      "total_order_count": 0,
      "pending_order_count": 0,
      "positions": [
        {
          "market_id": 0,
          "symbol": "ETH",
          "sign": 1,                    // 1 = long, -1 = short
          "position": "0.0000",
          "avg_entry_price": "0.00",
          "position_value": "0.000000",
          "unrealized_pnl": "0.000000",
          "realized_pnl": "0.000000",
          "liquidation_price": "0",
          "margin_mode": 0,             // 0 = cross, 1 = isolated
          "allocated_margin": "0.000000",
          "initial_margin_fraction": "5.00"
        }
      ],
      "assets": [
        {"symbol": "ETH", "asset_id": 1, "balance": "3.00000000", "locked_balance": "0.00000000"}
      ]
    }
  ]
}
```

Looking up by `l1_address` returns the same shape but may include multiple accounts (master + sub-accounts).

`positions[]` is filtered to currently-open positions (non-zero size) by default. The Lighter API actually returns one row per market the account has ever touched, keeping cumulative `realized_pnl` and `total_funding_paid_out` server-side even after a position is flat. Pass `--include_zero_positions` to get that full list — needed for lifetime-PnL analytics, funding audits, and tax exports; skip it for any "what am I holding right now" read.

---

## `account limits`

```json
{
  "code": 200,
  "user_tier": "standard",
  "user_tier_name": "standard",
  "current_maker_fee_tick": 0,
  "current_taker_fee_tick": 0,
  "max_llp_percentage": 100,
  "max_llp_amount": "0.000000",
  "leased_lit": "0.00000000",
  "effective_lit_stakes": "0.00000000",
  "can_create_public_pool": false
}
```

---

## `portfolio performance`

```json
{
  "code": 200,
  "resolution": "1h",
  "pnl": [
    {
      "timestamp": 1775606400,          // unix seconds
      "trade_pnl": -10000,
      "trade_spot_pnl": 57965.62,
      "inflow": 10000,
      "outflow": 0,
      "spot_inflow": 1022232.28,
      "spot_outflow": 0,
      "pool_pnl": 0,
      "staking_pnl": 0,
      "volume": 153880.65
    }
  ]
}
```

---

## `orders open` / `orders history`

```json
{
  "code": 200,
  "orders": [
    {
      "order_index": 281474983018026,
      "client_order_index": 79084886055,    // <- this is what cancel/modify takes
      "order_id": "281474983018026",
      "client_order_id": "79084886055",
      "market_index": 0,
      "owner_account_index": 41,
      "is_ask": true,                       // true = sell, false = buy
      "type": "market",                     // "limit" or "market"
      "time_in_force": "immediate-or-cancel",
      "status": "filled",                   // "new"/"open"/"filled"/"canceled"
      "initial_base_amount": "68.0066",
      "remaining_base_amount": "0.0000",
      "filled_base_amount": "68.0066",
      "filled_quote_amount": "146143.46",
      "price": "2146.65",
      "trigger_price": "0.00",
      "reduce_only": false,
      "order_expiry": 0,
      "nonce": 6307370
    }
  ]
}
```

`orders open` returns only currently-open orders for one market. `orders history` returns historical (filled, canceled, expired) orders, paginated by `cursor`.

---

## `auth status`

Local-only credential introspection. Never reaches the network and never loads the SDK, so it stays cheap on cold sessions.

```json
{
  "status": "ok",
  "auth_capable": true,
  "host": "https://mainnet.zklighter.elliot.ai",
  "account_index": 722851,
  "api_key_index": 123,
  "sources": {
    "LIGHTER_API_PRIVATE_KEY": "credentials_file",  // "env" | "credentials_file" | null
    "LIGHTER_ACCOUNT_INDEX": "credentials_file",
    "LIGHTER_API_KEY_INDEX": "credentials_file",
    "LIGHTER_HOST": "default"                       // adds "default" when no override
  },
  "credentials_file": {
    "path": "/Users/you/.lighter/lighter-agent-kit/credentials",
    "present": true,
    "mode_secure": true                             // true = owner-only (chmod 600); false = group/world can read; null when file missing
  },
  "missing": []                                     // names of any required vars not resolved
}
```

`auth_capable` is `true` iff `missing` is empty. The private-key value is never returned — only its source.
