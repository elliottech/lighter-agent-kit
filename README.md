# Lighter Agent Kit

[![Release](https://img.shields.io/github/v/release/elliottech/lighter-agent-kit?display_name=tag)](https://github.com/elliottech/lighter-agent-kit/releases)
[![License](https://img.shields.io/github/license/elliottech/lighter-agent-kit)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-macOS%20arm64%20%7C%20Linux%20x86__64%20%7C%20Linux%20arm64-0A7B83)](#install)
[![Python](https://img.shields.io/badge/python-3.9%2B-3776AB?logo=python&logoColor=white)](#install)

A skill that lets AI agents trade on [Lighter](https://lighter.xyz). Install it into Claude Code, Cursor, Codex, or any agent that implements the [agentskills.io](https://agentskills.io) spec, then interact with it in natural language.

Install in one-line:
```bash
curl -fsSL https://github.com/elliottech/lighter-agent-kit/releases/latest/download/install.sh | bash
```

> [!CAUTION]
> Live orders and withdrawals on Lighter are irreversible. Test strategies with paper trading first, and review [DISCLAIMER.md](DISCLAIMER.md) before using a funded account.

## Usage

Natural language is the primary interface. Just talk to your agent:

> *"Which perp has the tightest spread right now, BTC or ETH?"*
>
> *"Build a simple momentum strategy for BTC and run it on my paper account"*
>
> *"List current funding rates and flag anything above 10% annualized."*
>
> *"Place a limit buy for 0.01 ETH at $50 under the current mid."*

The agent resolves symbols, fetches market metadata and dispatches the appropriate scripts.

## Full Capabilities

| Capabilities                                                                      |
| --------------------------------------------------------------------------------- |
| Order books, candles, funding rates, recent fills, market metadata                |
| Paper trading (local simulation against live Lighter order books)                 |
| Account balances, positions, open orders, order history, PnL                      |
| Limit and market orders, modify, cancel, leverage, margin changes                 |
| Withdrawals and transfers between perp/spot collateral buckets                    |

Both Lighter market types are supported:

- **Perpetuals** — BTC, ETH, SOL, LIT, and more. Cross or isolated margin with leverage and funding.
- **Spot** — `ETH/USDC`, `LIT/USDC`, `LINK/USDC`, and others. Spot symbols are distinguished by the `/` separator.

Regulatory status of cryptocurrency trading varies by jurisdiction. It is your responsibility to comply with applicable laws.


## Install

```bash
curl -fsSL https://github.com/elliottech/lighter-agent-kit/releases/latest/download/install.sh | bash
```

The installer is served from the latest GitHub release, so it tracks published versions instead of the moving `main` branch. It detects your platform (macOS Apple Silicon, Linux x86_64, or Linux ARM64), installs Python if needed, and registers the skill with the agents you select. Restart your agent session afterward. The skill auto-triggers on trading prompts, or can be invoked explicitly with `/lighter-agent-kit`.

Verify the install by running the health check from the install directory (the installer prints it at the end):

```bash
python3 scripts/health.py
```

Manual installation:

```bash
git clone https://github.com/elliottech/lighter-agent-kit ~/.agents/skills/lighter-agent-kit
ln -s ~/.agents/skills/lighter-agent-kit ~/.claude/skills/lighter-agent-kit  # for Claude Code
```

Project-scoped install: replace `~/.agents` with `.agents` and `~/.claude` with `.claude`.


## API keys

Public reads work without credentials. Account-private reads and writes — orders, withdrawals, transfers — require a Lighter API key.

Generate a key at [app.lighter.xyz/apikeys](https://app.lighter.xyz/apikeys) — the private key is shown only once. Then run the configuration helper from the install directory:

```bash
./lighter-config
```

It prompts for your L1 address, resolves the account index via Lighter's API, reads your API private key, and writes `~/.lighter/lighter-agent-kit/credentials` at mode 0600. Re-run it any time to rotate keys or switch accounts.

For CI or manual setup, export the values directly:

```bash
export LIGHTER_API_PRIVATE_KEY=<private key>
export LIGHTER_ACCOUNT_INDEX=<account index>
export LIGHTER_API_KEY_INDEX=<key index>
```

Environment variables take precedence over the credentials file. For testnet, add `export LIGHTER_HOST=https://testnet.zklighter.elliot.ai`. Full configuration reference: [references/env-vars.md](references/env-vars.md).

Never commit private keys to any repository.

## Scripts

The skill is implemented as three Python scripts:


| Script             | Role                                                  |
| ------------------ | ----------------------------------------------------- |
| `scripts/query.py` | Market data, public account reads, and authenticated reads |
| `scripts/trade.py` | Signed writes against live Lighter                    |
| `scripts/paper.py` | Local simulation (perps only, market/IOC orders only) |


All three follow the pattern `<group> <action> [args]`, emit JSON to stdout, and return failures as `{"error": "..."}`. Each subcommand supports `--help`. Run them directly when debugging agent behavior or scripting against the skill:

```bash
cd ~/.agents/skills/lighter-agent-kit

python3 scripts/query.py market book BTC --limit 10
python3 scripts/trade.py order limit BTC --side long --amount 0.01 --price 60000
python3 scripts/paper.py init
python3 scripts/paper.py order market BTC --side long --amount 0.1
```

Paper trading requires `python3 scripts/paper.py init` once per new paper-state file before the first order. State persists at `~/.lighter/lighter-agent-kit/paper-state.json`; reset it with `python3 scripts/paper.py reset`.

## Command index

### query.py — reads


| Command                                   | Auth     | Purpose                              |
| ----------------------------------------- | -------- | ------------------------------------ |
| `system status`                           | —        | Network health                       |
| `market list`                             | —        | Symbol catalog                       |
| `market stats [--symbol X]`               | —        | Prices, 24h volume                   |
| `market info [--symbol X]`                | —        | Fees, decimals, minimum sizes        |
| `market book <symbol>`                    | —        | Top-of-book snapshot                 |
| `market trades <symbol>`                  | —        | Recent fills                         |
| `market candles <symbol> --resolution 1h` | —        | OHLCV (1m, 5m, 15m, 30m, 1h, 4h, 1d) |
| `market funding --symbol X`               | —        | Funding rate                         |
| `account info [--account_index N]`        | Optional | Public account lookup                |
| `account apikeys [--account_index N]`     | Optional | Public API-key listing               |
| `account limits`                          | ✓        | Tier and trading limits              |
| `portfolio performance --resolution 1h`   | ✓        | PnL series                           |
| `orders open --symbol X`                  | ✓        | Live open orders                     |
| `orders history`                          | ✓        | Past orders                          |
| `auth status`                             | —        | Local credential check               |

`account info` and `account apikeys` are public reads when you pass `--account_index`. The authenticated reads are `account limits`, `portfolio performance`, `orders open`, and `orders history`.


### trade.py — writes

Amounts and prices are human units; the script loads market metadata and scales to integer ticks before signing.


| Command                                                           | Notes                                       |
| ----------------------------------------------------------------- | ------------------------------------------- |
| `order limit <symbol> --side S --amount N --price N`              | `--reduce_only` and `--post_only` supported |
| `order market <symbol> --side S --amount N`                       | 1% default slippage budget                  |
| `order modify <symbol> --order_index COI --price N --amount N`    | Keyed on `client_order_index`               |
| `order cancel <symbol> --order_index COI`                         | Single-order cancel                         |
| `order cancel_all`                                                | Every open order, every market              |
| `order close_all [--slippage N] [--with_cancel_all] [--preview]`  | Flatten every open position                 |
| `position leverage <symbol> --leverage N`                         | `--margin_mode cross\|isolated`             |
| `position margin <symbol> --amount N --direction add\|remove`     | Adjust isolated margin                      |
| `funds withdraw --asset A --amount N`                             | `--route perp\|spot`                        |
| `funds transfer --asset A --amount N --from_route X --to_route Y` | Between your perp and spot collateral       |


`--side` accepts `buy`, `sell`, `long`, or `short` on both market types. Order-creation calls return a `client_order_index` — retain it as the handle for subsequent modify and cancel calls.

### paper.py — simulation

Local simulation against live Lighter order book snapshots. Perps only; taker-only fills (market and IOC). State at `~/.lighter/lighter-agent-kit/paper-state.json`.


| Command                                           | Purpose                                              |
| ------------------------------------------------- | ---------------------------------------------------- |
| `init`                                            | Create a new paper account (required before orders) |
| `reset`                                           | Wipe paper state and start fresh                     |
| `set_tier --tier T`                               | Change fee tier (`standard`, `premium`, `premium_1`…`premium_7`) |
| `status`                                          | Account summary — equity, margin, fees              |
| `positions [--symbol X] [--no-refresh]`           | Open paper positions                                 |
| `trades [--symbol X] [--limit N]`                 | Paper trade history (most recent first)              |
| `health`                                          | Account health and margin status                     |
| `liquidation_price <symbol> [--no-refresh]`       | Estimated liquidation price for a position           |
| `refresh <symbol>`                                | Force-refresh order book snapshot (diagnostic)       |
| `order market <symbol> --side S --amount N`       | Taker-only market order                              |
| `order ioc <symbol> --side S --amount N --price N`| Taker-only IOC with limit price                      |


`--side` accepts `buy`, `sell`, `long`, or `short`. `--no-refresh` uses cached mark prices for faster reads.

## Agent integration

The authoritative agent-facing contract is [SKILL.md](SKILL.md): invocation rules, auth flow, error envelopes, paper-trading caveats, symbol conventions. Response shapes are catalogued in [references/schemas-read.md](references/schemas-read.md), [references/schemas-write.md](references/schemas-write.md), and [references/schemas-paper.md](references/schemas-paper.md).

## License

MIT — see [LICENSE](LICENSE). This is experimental software; submitted orders and withdrawals cannot be reversed. Full terms in [DISCLAIMER.md](DISCLAIMER.md).
