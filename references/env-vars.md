# Environment Variables

## Read-only commands

| Variable | Required | Description |
|----------|----------|-------------|
| `LIGHTER_HOST` | No | API base URL (default: `https://mainnet.zklighter.elliot.ai`) |

## Write commands and account-private reads

| Variable | Required | Description |
|----------|----------|-------------|
| `LIGHTER_API_PRIVATE_KEY` | Yes | API signing key (hex, 80 chars) |
| `LIGHTER_ACCOUNT_INDEX` | Yes | Lighter L2 account index |
| `LIGHTER_API_KEY_INDEX` | Yes | Which API key slot to use, 0–255 |
| `LIGHTER_HOST` | No | API base URL (default: mainnet) |
| `LIGHTER_ETH_PRIVATE_KEY` | Privileged ops only | L1 ETH key for `change_api_key`/`transfer` — **not exposed by this skill** (see the capability table in [SKILL.md](../SKILL.md#what-capabilities-are-allowed)) |

Read commands that hit account-private data (`account_limits`, `pnl`, `account_active_orders`, `account_inactive_orders`) generate a short-lived auth token from `LIGHTER_API_PRIVATE_KEY` + `LIGHTER_ACCOUNT_INDEX` on the fly — no separate token management required.

## Paper trading

| Variable | Required | Description |
|----------|----------|-------------|
| `LIGHTER_PAPER_STATE_PATH` | No | Override for paper state file path (default: `~/.lighter/lighter-agent-kit/paper-state.json`) |
| `LIGHTER_HOST` | No | Paper mode pulls real order book snapshots from this host (default: mainnet) |

Paper trading does not require `LIGHTER_API_PRIVATE_KEY`, `LIGHTER_ACCOUNT_INDEX`, or `LIGHTER_API_KEY_INDEX` — all state is local.

If required credentials are missing, the script returns a clear `{"error": "missing LIGHTER_..."}` and exits 1.
## Environments

| Environment | `LIGHTER_HOST` value |
|---|---|
| Mainnet | `https://mainnet.zklighter.elliot.ai` (default) |
| Testnet | `https://testnet.zklighter.elliot.ai` |
| Staging | `https://staging.zklighter.elliot.ai` |

Always test on testnet or staging before running mainnet writes.

## Credentials

Public reads need no credentials.

For account-private reads and write commands, you have two options:

1. Set environment variables before launching your agent:

```bash
export LIGHTER_API_PRIVATE_KEY=...
export LIGHTER_ACCOUNT_INDEX=...
export LIGHTER_API_KEY_INDEX=...
```

2. Put them in your personal `lighter-agent-kit` credentials file:

- `~/.lighter/lighter-agent-kit/credentials`

Example:

```text
LIGHTER_API_PRIVATE_KEY=...
LIGHTER_ACCOUNT_INDEX=...
LIGHTER_API_KEY_INDEX=...
LIGHTER_HOST=https://testnet.zklighter.elliot.ai
```

If you set both, environment variables win.

Do not put private keys inside this repo or the skill folder.

## Inspecting your config

```bash
python3 scripts/query.py auth status
```

Reports `auth_capable` plus the resolved source per credential. Covers both env vars and the credentials file. See [schemas-read.md](schemas-read.md#auth-status) for the full response shape.

## Security

- The credentials file should be readable only by the owner. Set `chmod 600 ~/.lighter/lighter-agent-kit/credentials`. The skill warns to stderr if it detects overly permissive file modes.
- `LIGHTER_API_PRIVATE_KEY` and `LIGHTER_ETH_PRIVATE_KEY` are loaded as `SecretValue` objects. `str()` and `repr()` always return `[REDACTED]`. Use `.expose()` only inside signing code — never log the result.
- **Agents must never read the credentials file or echo secret env vars.** Manual truthiness probes are wrong because they miss the credentials-file source.
