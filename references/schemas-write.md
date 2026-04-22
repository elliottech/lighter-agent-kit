# Write Command Responses (`trade.py`)

## Success envelope

All write commands return the same envelope on success:

```json
{
  "status": "submitted",
  "tx_hash": "c3a0c377c6367857fb36a23aa6d0132978728ddd336cb07a7e3a6d4c3cfa53ee067d43ab4fea5f4b",
  "tx": { /* original signed tx body — present for every write command */ }
}
```

`submitted` means the signed transaction was accepted by the API and assigned a `tx_hash`; it does **not** guarantee the order later filled or rested on the book. For IOC/market-style writes, check order history for the final exchange status if you need execution certainty.

`order limit` and `order market` additionally include `client_order_index` and `side` (normalized). **Save `client_order_index`** — it is the only handle for later modifying or cancelling that order.

---

## Precision echoback

`order limit`, `order market`, and `order modify` echo the values that were *actually* sent to the exchange after rounding to the market's tick/lot precision:

```json
{
  "status": "submitted",
  "tx_hash": "...",
  "client_order_index": 12345,
  "effective_amount": "0.0057",
  "effective_price": "4050.57",
  "tx": { ... }
}
```

If you requested `--price 4050.567 --amount 0.00567` on a 2-decimal price / 4-decimal size market, you'll see `effective_price: "4050.57"` and `effective_amount: "0.0057"`. Use these to confirm what landed on the order book.

Market decimals come from the `supported_size_decimals` and `supported_price_decimals` fields in [schemas-read.md → market info](schemas-read.md#market-info). The script fetches them automatically before scaling.

---

## `order close_all`

Flattens every open position with per-market reduce-only market orders. Returns a summary envelope instead of the standard `{tx_hash, tx}` shape, because one call can submit multiple signed txs in a single `sendTxBatch` request.

Preview (no broadcast):

```json
{
  "status": "ok",
  "preview": true,
  "would_close": [
    {
      "symbol": "BTC",
      "market_id": 1,
      "current_side": "long",
      "closing_side": "short",
      "amount": "0.00050"
    }
  ]
}
```

Preview with `--with_cancel_all`:

```json
{
  "status": "ok",
  "preview": true,
  "note": "--with_cancel_all would cancel all resting orders before sending the close batch",
  "would_close": [
    {
      "symbol": "BTC",
      "market_id": 1,
      "current_side": "long",
      "closing_side": "short",
      "amount": "0.00050"
    }
  ]
}
```

Execute:

```json
{
  "status": "ok",
  "closed": [
    {
      "symbol": "BTC",
      "market_id": 1,
      "closing_side": "short",
      "amount": "0.00050",
      "client_order_index": 1776654321000,
      "tx_hash": "..."
    }
  ],
  "failed": [],
  "cancelled_orders_first": false
}
```

When `--with_cancel_all` is used, the cancel-all pre-step is signed into the same batch ahead of the closing orders. In that case the response also includes:

```json
{
  "cancel_all_tx_hash": "..."
}
```

Rows in `account info.positions[]` with `position == 0` (markets the account has previously touched, kept server-side for cumulative realized_pnl / funding history) are silently skipped and not reported.

`status` values:
- `ok` — every non-zero position was closed and the cancel-all step (if requested) succeeded
- `partial` — at least one close succeeded and at least one close OR the cancel-all step failed
- `error` — nothing succeeded (every close attempt failed, or there were no positions and only the cancel-all step ran and failed)

Per-market preparation failures (missing decimals, size rounds to zero, best-price lookup failure) are caught individually and appended to `failed[]`. Prepared closes are then signed and broadcast together via one batch request. If any single close fails to sign, the whole batch is aborted: the failing market is appended to `failed[]` with the signer error, and any closes already signed in that batch are appended to `failed[]` with an `"aborted before send: a later sign failed"` note. If the batch send itself is rejected, every prepared close is reported in `failed[]` with the same batch error and none of them are reported in `closed[]`.

`cancelled_orders_first` echoes whether `--with_cancel_all` was requested on executed calls. In preview mode, the response uses `note` instead of `cancelled_orders_first` to indicate that cancel-all would run before the close batch. `cancel_all_tx_hash` is only present when the cancel-all actually broadcast (i.e. the batch send succeeded); if the batch was rejected, `cancelled_orders_first` may be true while `cancel_all_tx_hash` is absent — meaning resting orders were **not** cancelled. When the cancel-all step fails (either at sign time or when the batch send is rejected), a top-level `cancel_all_error` is added with the cleaned error message.

`warning` (optional, top-level): present **only** when `cancel_all_tx_hash` is set AND `failed[]` is non-empty. It means resting orders (including TP/SL brackets) were cancelled but at least one position is still open with no protection. Surface this to the user prominently and recommend an immediate re-run of `order close_all` (or manual bracket replacement).

---

## Error shape

On failure:

```json
{"error": "order limit failed: not enough margin to create the order"}
```

The error message is the cleaned-up `message='...'` field from the API response. Common fragments:

| Message fragment | Likely cause |
|---|---|
| `not enough margin` | Account collateral too low for the requested order |
| `not enough collateral` | Withdraw amount exceeds available balance |
| `invalid margin mode` | `position margin` called on a cross-margin position (only valid on isolated) |
| `invalid nonce` | Transient — the signer auto-refreshes; retry once if it persists |
| `order not found` | `order cancel` / `order modify` with a stale or wrong `--order_index` |
| `unknown symbol` / `market_index N not found` | The symbol or index doesn't exist; run `query.py market list` to discover valid markets |
| `missing LIGHTER_...` | Required env var not set; see [env-vars.md](env-vars.md) |

Errors always exit with code `1`. The script never emits a Python traceback — unexpected exceptions are caught and reshaped into the `{"error": "..."}` envelope.
