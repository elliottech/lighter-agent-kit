"""Shared per-user path policy for lighter-agent-kit local data."""

import hashlib
import os
from pathlib import Path


def lighter_agent_kit_data_dir():
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "lighter-agent-kit"
    return Path.home() / ".lighter" / "lighter-agent-kit"


def credentials_path():
    return lighter_agent_kit_data_dir() / "credentials"


def default_paper_state_path():
    return lighter_agent_kit_data_dir() / "paper-state.json"


def paper_state_path():
    override = os.environ.get("LIGHTER_PAPER_STATE_PATH")
    if override:
        return Path(override)
    return default_paper_state_path()


def symbol_cache_path(host: str):
    normalized = host.strip().lower().rstrip("/")
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return lighter_agent_kit_data_dir() / f"symbol-cache-{digest}.json"
