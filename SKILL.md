---
name: lighter-agent-kit
description: >-
  Query Lighter DEX market data (orderbooks, trades, candles, funding rates)
  and execute trades (limit and market orders, modify, cancel, leverage,
  margin, withdraw, spot and perp transfers) on perpetual and spot markets.
  Use when the user mentions Lighter, zkLighter, perps, perpetual futures,
  or wants to check positions or PnL or place orders on Lighter.
allowed-tools:
  - Bash
compatibility: >-
  Requires Python 3.9+. Supported platforms: macOS arm64, Linux x86_64,
  Linux arm64. Intel Macs (darwin-x86_64) are NOT supported because
  lighter-sdk does not ship a native signer binary for that target.
---

# Lighter Agent Kit

Trade on Lighter — a ZK-rollup perpetual futures and spot exchange.

Scripts live in this skill's `scripts/` directory. Read commands use `query.py`, write commands use `trade.py`. Every command prints structured JSON to stdout. Errors are always in JSON as `{"error": "..."}`.

Commands use `<group> <action>` syntax mirroring the Lighter UI (e.g. `market stats`, `order limit`, `orders open`).

For response schemas and details, see the [references/](references/) folder.

## Install

Copy this folder to one of the standard Claude Code skill locations:

- **Personal (all projects):** `~/.claude/skills/lighter-agent-kit/`
- **Project-level (this repo only):** `.claude/skills/lighter-agent-kit/`

On first call, `lighter-sdk` and its transitive deps will be installed into `<skill>/.vendor/pyX.Y/`. Every version is pinned by `requirements.lock`. Supported targets are Apple Silicon Macs plus Linux x86_64/arm64. **Intel** Macs are not supported because lighter-sdk doesn't ship a darwin-x86_64 signer binary.

Requires network egress to `mainnet.zklighter.elliot.ai` / `testnet.zklighter.elliot.ai`, `pypi.org` / `files.pythonhosted.org`, and `github.com` (pip clones the pinned `lighter-sdk` build from GitHub). On Claude.ai's sandbox the default egress allowlist doesn't include `github.com` — you'll need to expand it before first run.

Read commands work with no credentials. Write commands and account-private reads will first check environment variables and then your personal credentials file — see [references/env-vars.md](references/env-vars.md).

## How to Handle User Requests

1. **Symbol convention: perp-bare, spot-pair.** Perp markets use bare tickers (`BTC`, `ETH`, `SOL`, `LIT`); spot markets use quote-qualified pairs (`ETH/USDC`, `LIT/USDC`, `LINK/USDC`). The presence of `/` is the single discriminator — no `--market_type` flag is needed on symbol-resolved commands. Numeric `market_index` is also accepted as an escape hatch.
2. **Symbol resolution is automatic.** Symbols resolve from the live order-books API and are cached on disk for 5 minutes per host, so `query.py`, `trade.py`, and `paper.py` share the same market map across calls. Only when the user says something you can't confidently map (e.g. "brent oil", "gold", "silver"), run `python3 scripts/query.py market list --search <substring> [--market_type perp|spot]` first to discover it. These usually appear under ticker-style symbols like `BRENTOIL`, `XAU`, and `XAG`.
3. **Side accepts both forms.** `--side buy|sell|long|short` — both are accepted on perp and spot. Normalized internally to canonical (long/short for perp, buy/sell for spot).
4. **Filter at the source on high-cardinality reads.** `market funding`, `market stats`, and `market info` accept `--symbol` / `--market_index` / `--exchange` — always pass them when you only need one row.
5. Run the matching script: `python3 scripts/query.py <group> <action> ARGS` for reads, `python3 scripts/trade.py <group> <action> ARGS` for writes. Paths are relative to this skill's directory.
6. Parse the JSON and present it clearly.
7. **Account-scoped commands and `LIGHTER_ACCOUNT_INDEX`.** Two cases:
   - **Public reads** (`account info`, `account apikeys`) accept `--account_index` and default to `LIGHTER_ACCOUNT_INDEX` when omitted.
   - **Authenticated reads** (`account limits`, `portfolio performance`, `orders open`, `orders history`) are **self-only** using `LIGHTER_ACCOUNT_INDEX` instead of `--account_index` because the auth token is bound to that account.

**Diagnostic (optional):** if a call fails with an `import lighter` style error, run `python3 scripts/bootstrap.py` to verify the SDK vendored correctly. It should return a response with `status: ok` field. You should never need to run this in a healthy session.

---

## Read Commands (`query.py`)

### Public reads — no credentials

| Command | Purpose |
|---|---|
| `system status` | zkLighter system status (network id, timestamp). |
| `market list [--market_type perp\|spot] [--search X]` | Compact `{symbol, market_index, market_type}` catalog. Use for symbol → index lookups. |
| `market stats [--symbol X]` | Market overview: prices, 24h volumes, daily trades. Pass `--symbol` to get one row. |
| `market info [--market_type perp\|spot] [--symbol X]` | Full market metadata (fees, decimals, min sizes). Always filter; unfiltered is ~150 rows. |
| `market book <symbol> [--limit 20]` | Top-of-book bids and asks. `<symbol>` is perp ticker (`BTC`), spot pair (`ETH/USDC`), or numeric market_index. `--limit` is per side. |
| `market trades <symbol> [--limit 20]` | Recent fills for one market. `--limit` max 100. |
| `market candles <symbol> --resolution 1h --count_back 24` | OHLCV candles. Resolutions: `1m`, `5m`, `15m`, `30m`, `1h`, `4h`, `1d`. Returns `o`/`h`/`l`/`c` (price), `v` (base volume), `V` (quote volume), `t` (bucket start, ms), `i` (last trade id). |
| `market funding [--symbol X] [--market_index N] [--exchange X]` | Funding rates across venues. ~300 rows unfiltered; always pass `--symbol` or `--exchange`. |
| `account info [--account_index N] [--by l1_address --value 0x…] [--include_zero_positions]` | Account lookup: balances, positions, assets. Defaults to `LIGHTER_ACCOUNT_INDEX`. Use `--by l1_address --value …` to look up by L1 address instead. `positions[]` is filtered to currently open positions by default — Lighter keeps one row per market the account has ever touched so the raw list can be huge and mostly flat. Pass `--include_zero_positions` when you need lifetime `realized_pnl` / `total_funding_paid_out` per market (PnL analytics, funding audits, tax reports). |
| `account apikeys [--account_index N] [--api_key_index N]` | List API keys for an account. Omit `--api_key_index` (defaults to 255) to list all keys. |
| `auth status` | Local-only credential precheck. Returns `auth_capable` + source per credential. Run once before authenticated calls. |

### Account-private reads — require `LIGHTER_API_PRIVATE_KEY` + `LIGHTER_ACCOUNT_INDEX` + `LIGHTER_API_KEY_INDEX`

These endpoints generate a short-lived auth token from your configured credentials on every call. The auth token is bound to `LIGHTER_ACCOUNT_INDEX`, so all four commands below are **self-only** — they do not accept `--account_index` and there is no way to query a different account from the one whose private key signed the token.

| Command | Purpose |
|---|---|
| `account limits` | Trading limits and tier info. |
| `portfolio performance --resolution 1h --count_back 24` | PnL chart over a time range. |
| `orders open (--symbol X \| --market_index N)` | Open orders on one market. Pass either `--symbol` (perp ticker or spot pair) or `--market_index`. |
| `orders history [--symbol X] [--market_index N] [--limit 20]` | Filled / canceled order history. Market filter is optional. |

> **Platform note:** on **claude.ai's sandboxed bash environment** there is no persistent shell to `export` env vars into, and pasting an API private key into chat is not acceptable. Treat the four commands above as effectively Claude Code / Cursor / desktop only. On claude.ai, fall back to the public reads or direct the user to the Lighter web/mobile UI for account-private views.

Sanity check: `python3 scripts/health.py` returns `{"status": "ok", "version": "..."}` without touching the SDK — useful to confirm the skill loads at all.

See [references/schemas-read.md](references/schemas-read.md) for the full response shapes.

---

## Write Commands (`trade.py`)

**All write commands require credentials.** Prices and amounts are in human units (e.g. `--price 4050.5 --amount 0.1`); the script fetches market metadata and scales them to integer ticks/lots automatically. The response echoes `effective_amount` / `effective_price` so you can see what actually landed after rounding.

**Side accepts both forms:** `--side buy|sell|long|short`. Both are accepted on perp and spot markets. Normalized internally to canonical (long/short for perp, buy/sell for spot).

**Symbol convention:** The positional `<symbol>` argument is a perp ticker (`BTC`, `ETH`), a spot pair (`ETH/USDC`, `LIT/USDC`), or a numeric `market_index` (`1`). The `/` in a spot symbol is the discriminator — no `--market_type` flag is needed.

**Spot trading:** every order command works transparently for spot markets — just pass a spot pair like `ETH/USDC`. Use `query.py market list --market_type spot` to discover them. On spot markets, `--reduce_only` and the `position leverage` / `position margin` commands are no-ops — spot has no leveraged positions.

| Command | Key arguments |
|---|---|
| `order limit <symbol> --side <side> --amount N --price N [--reduce_only] [--post_only]` | Limit order on perp or spot. |
| `order market <symbol> --side <side> --amount N [--slippage N] [--reduce_only]` | Market order on perp or spot. Default slippage `0.01` (1%). |
| `order modify <symbol> --order_index COI --price N --amount N` | `COI` is the `client_order_index` returned by `order limit`. |
| `order cancel <symbol> --order_index COI` | Cancel a single open order. |
| `order cancel_all` | Cancel every open order across every market (no args). |
| `order close_all [--slippage N] [--with_cancel_all] [--preview]` | **High-risk.** Flattens every open position with reduce-only market orders — realizes PnL on every market at once. Always run `--preview` first and get explicit user approval before the real call; do not auto-infer intent from phrases like "clean up" or "reset". `--with_cancel_all` also kills TP/SL brackets. When combined with `--preview`, the response includes a note that cancel-all would run first, but `would_close[]` still lists positions only. |
| `position leverage <symbol> --leverage N [--margin_mode cross\|isolated]` | Set leverage. Default `cross`. |
| `position margin <symbol> --amount N --direction add\|remove` | Amount is USDC; only valid on isolated positions. |
| `funds withdraw --asset A --amount N [--route perp\|spot]` | Default route `perp`. Assets: usdc, eth, lit, link, uni, aave, sky, ldo. This creates a withdrawal request; funds may move into a pending / claim flow and not appear in the wallet instantly. |
| `funds transfer --asset A --amount N --from_route perp\|spot --to_route perp\|spot` | Moves assets between your own spot and perp buckets. No L1 signature, no cross-account routing. |

`order limit` and `order market` return a `client_order_index` — **save it**; it is the handle for later `order modify` / `order cancel`.

**Example — move USDC from perp to spot to fund a spot buy:**

```bash
python3 scripts/trade.py funds transfer --asset usdc --amount 250 \
    --from_route perp --to_route spot
```

Every command supports `--help`. See [references/schemas-write.md](references/schemas-write.md) for the response envelope, precision echoback, and error-code table.

---

## Paper Trading (`paper.py`)

Simulate trades against real Lighter order book snapshots without credentials, without broadcasting, and without risk. All state is local.

Shared-subset commands use `<group> <action>` shape identical to `trade.py`, so swapping the script name swaps the engine. Paper-only lifecycle commands stay flat.

### Commands

**Paper-only (flat):**

| Command | Purpose |
|---|---|
| `init [--collateral N] [--tier T]` | Create paper account. Defaults: `--collateral 10000 --tier premium`. |
| `reset [--collateral N] [--tier T]` | Wipe state. No-args form reuses previous collateral and tier. |
| `set_tier --tier T` | Change fee tier on existing account (updates cached market configs). |
| `status [--no-refresh]` | Account summary: collateral, PnL, position/trade counts. Auto-refreshes mark prices for all open positions; pass `--no-refresh` to skip. |
| `positions [--symbol X] [--no-refresh]` | Open positions. No `--symbol` lists all. Auto-refreshes mark prices. |
| `trades [--symbol X] [--limit 50]` | Trade history, most recent first. |
| `health [--no-refresh]` | Account health: TAV, margin requirements, leverage. Auto-refreshes mark prices. |
| `liquidation_price <symbol> [--no-refresh]` | Estimated liq price for a position. Auto-refreshes mark prices. |
| `refresh <symbol>` | Force-refresh one market's order book snapshot. Useful for pre-warming a market before trading, or when you want current mark data without passing `--no-refresh` on every read. |

**Shared subset (mirrors trade.py shape):**

| Command | Purpose |
|---|---|
| `order market <symbol> --side buy\|sell\|long\|short --amount N` | Taker market order against live book snapshot. |
| `order ioc <symbol> --side buy\|sell\|long\|short --amount N --price N` | Taker IOC order with limit price. |

**Swap rule:** swap `trade.py` ↔ `paper.py` to switch engines:
```bash
# live
python3 scripts/trade.py order market BTC --side long --amount 0.1

# paper — same call, same args, no live broadcast
python3 scripts/paper.py order market BTC --side long --amount 0.1
```

Tiers: `standard` (0/0 bps), `premium` (2.8/0.4 bps, default), `premium_1` through `premium_7`. See [references/schemas-paper.md](references/schemas-paper.md) for full tier table and response shapes.

### Paper vs Live Caveats

These structural simulator limits cause paper-to-live PnL divergence, roughly ordered by how often they bite:

1. **Maker fills never occur.** Every paper fill is charged taker fee, even an IOC at the limit price. Live resting limits would pay maker and often get a better effective price.
2. **No order-impact model.** Paper orders walk the real book but don't move it. A size that clears 3 levels in paper would, live, push those levels and make the next entry worse.
3. **No latency / partial-fill modeling.** Paper fills instantly in full if depth exists. Live orders can miss entries or fill partially.
4. **No funding accrual.** For positions held across funding intervals, this is typically the dominant PnL drag/credit. Paper ignores it.
5. **Cross-margin only.** Strategies depending on isolated-margin blast-radius containment cannot be paper-validated.

Secondary items:

- No credentials required for paper mode
- Perp-only (`market_index >= 2048` is rejected)
- Only `MARKET` and `IOC` order types (no resting limits, no stop, no TP, no post-only)
- Local-only state; `rm "$LIGHTER_PAPER_STATE_PATH"` (or the default path `~/.lighter/lighter-agent-kit/paper-state.json`) is a valid nuclear reset

---

## Environment Variables

See [references/env-vars.md](references/env-vars.md). TL;DR: public reads need nothing; for account-private reads and write commands, set `LIGHTER_API_PRIVATE_KEY`, `LIGHTER_ACCOUNT_INDEX`, and `LIGHTER_API_KEY_INDEX` as env vars or in your personal credentials file.

## Safety Notes

- Write commands sign and broadcast immediately. Claude Code's per-tool approval prompt is the user's confirmation step — the user sees the exact command before it runs.
- `funds withdraw` and order amounts are validated client-side (`> 0`, market exists, precision fits).
- **`funds withdraw` is not necessarily an instant wallet credit.** A successful response means the withdrawal request was accepted by Lighter. The balance may leave the selected route immediately, while the actual funds move through Lighter's withdrawal / pending-balance / claim flow before showing up in the user's wallet.
- **`order close_all` is a high-impact, account-wide write.** Treat it like `funds withdraw`: require explicit user approval before running the non-preview form, always run `--preview` first and show the plan to the user, and never infer intent from vague prompts ("tidy up", "reset my account", "start fresh"). Ask the user to confirm in plain language before the real call.
- Mainnet and testnet are toggled with `LIGHTER_HOST`. Use `https://testnet.zklighter.elliot.ai` for testing before mainnet.
- **First call installs `lighter-sdk` into `<skill>/.vendor/pyX.Y/`.** Expect ~15–40s on a cold session; subsequent calls are instant. Nothing is written outside the skill folder, so uninstalling is `rm -rf <skill>/.vendor`.

### Credential Security — MANDATORY

**Never read, cat, print, echo, or display the credentials file or any environment variable containing a private key.** This includes:
- `cat ~/.lighter/lighter-agent-kit/credentials`
- `echo $LIGHTER_API_PRIVATE_KEY`
- `env | grep LIGHTER`
- Reading `.envrc` or `.env` files
- Any other command that would output secret values to stdout/stderr

Never write your own env-var probe (`echo $...`, `python3 -c "print(os.environ.get(...))"`) — they miss the credentials file and risk leaking the secret. Secret values inside the SDK are wrapped in `SecretValue` objects that print `[REDACTED]`; do not attempt to bypass this.

## What capabilities are allowed

| Capability | Scope | Status |
|---|---|---|
| Order ops | create / modify / cancel / cancel_all / close_all, leverage, isolated margin | Supported |
| Self-routing | `funds transfer` between your own spot and perp buckets | Supported |
| Sub-account routing | `transfer` between master and sub-accounts | Not supported |
| Cross-account transfer | `transfer` to an arbitrary destination account | Not supported — transferring to arbitrary destinations is risky for unsupervised LLMs; advise the user to do it through the web or mobile interface. |
| API key rotation | `change_api_key` | Not supported — rotation requires `LIGHTER_ETH_PRIVATE_KEY` and a mistake locks the user out of their account; advise the user to do it through the web or mobile interface. |
