"""
Makes `import lighter` work by lazy-installing deps from `requirements.lock`
into `.vendor/pyX.Y/` on first use. lighter-sdk itself is just another line
in the lockfile — its universal wheel ships the signer binaries as package
data, so pip delivers them alongside the ABI-specific wheels (pydantic-core,
aiohttp, …).
"""

import json
import logging
import os
import subprocess
import sys
import warnings

from _paths import credentials_path

# Suppress third-party noise that would otherwise leak to stderr during
# the first `import lighter` in this process. urllib3 emits a
# NotOpenSSLWarning on macOS system Python (LibreSSL vs OpenSSL), and
# aiohttp leaks ResourceWarnings when partially-constructed sessions
# get GC'd (e.g. on a bad API key). Both fire during module import, so
# the filter must be set *before* ensure_lighter() triggers the chain.
warnings.filterwarnings("ignore")
warnings.simplefilter("ignore", ResourceWarning)
logging.getLogger("aiohttp").setLevel(logging.CRITICAL)
logging.getLogger("asyncio").setLevel(logging.CRITICAL)
logging.getLogger("urllib3").setLevel(logging.CRITICAL)

_SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PY_TAG = f"py{sys.version_info.major}.{sys.version_info.minor}"
VENDOR_DIR = os.path.join(_SKILL_DIR, ".vendor", _PY_TAG)
LOCKFILE = os.path.join(_SKILL_DIR, "requirements.lock")
DEFAULT_HOST = "https://mainnet.zklighter.elliot.ai"
_CREDENTIALS = None

# Keys whose values must never appear in logs, tracebacks, or agent output.
_SECRET_NAMES = frozenset({"LIGHTER_API_PRIVATE_KEY", "LIGHTER_ETH_PRIVATE_KEY"})


class SecretValue:
    """Opaque wrapper that keeps secret strings out of Debug/Display output."""

    __slots__ = ("_val",)

    def __init__(self, val: str):
        self._val = val

    def expose(self) -> str:
        """Return the plaintext secret. Callers must not log the result."""
        return self._val

    def __repr__(self):
        return "SecretValue([REDACTED])"

    def __str__(self):
        return "[REDACTED]"

    def __bool__(self):
        return bool(self._val)

# Floor is driven by the pinned deps in requirements.lock — pydantic,
# aiohttp, yarl and ~14 others all declare Requires-Python >=3.9. Without
# this guard, an old-Python caller would get an opaque pip resolver error
# mid-install instead of a clean "upgrade your Python" message up front.
MIN_PYTHON = (3, 9)


def _strip_optional_quotes(value):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _load_credentials():
    global _CREDENTIALS
    if _CREDENTIALS is not None:
        return _CREDENTIALS

    path = credentials_path()
    if not path.is_file():
        _CREDENTIALS = {}
        return _CREDENTIALS

    # Warn if the credentials file is readable by group or others.
    if os.name != "nt":
        try:
            mode = path.stat().st_mode
            if mode & 0o077:
                import stat

                print(
                    f"WARNING: {path} is accessible by other users "
                    f"(mode {stat.filemode(mode)}). "
                    f"Run: chmod 600 {path}",
                    file=sys.stderr,
                )
        except OSError:
            pass

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        _CREDENTIALS = {}
        return _CREDENTIALS

    creds = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = _strip_optional_quotes(value.strip())
        if key and value:
            creds[key] = SecretValue(value) if key in _SECRET_NAMES else value
    _CREDENTIALS = creds
    return _CREDENTIALS


def get_config_value(name, default=None):
    """Resolve a setting from env first, then the per-user credentials file.

    Returns a ``SecretValue`` for keys in ``_SECRET_NAMES``, forcing callers
    to explicitly call ``.expose()`` before passing the plaintext anywhere.
    """
    value = os.environ.get(name)
    if value:
        return SecretValue(value) if name in _SECRET_NAMES else value

    # Values from the credentials file are already wrapped by _load_credentials().
    value = _load_credentials().get(name)
    if value:
        return value

    return default


def resolve_with_source(name):
    """Like ``get_config_value`` but also report where the value came from.

    Returns ``(value, source)`` where ``source`` is ``"env"``,
    ``"credentials_file"``, or ``None`` when unset. Values for keys in
    ``_SECRET_NAMES`` come back as ``SecretValue`` — do not expose.
    Used by ``query.py auth status`` for credential introspection.
    """
    value = os.environ.get(name)
    if value:
        return (
            SecretValue(value) if name in _SECRET_NAMES else value,
            "env",
        )
    value = _load_credentials().get(name)
    if value:
        return value, "credentials_file"
    return None, None


def _stub_eth_account():
    """Inject a stub for eth_account into sys.modules.

    lighter-sdk's signer_client.py has a top-level ``from eth_account import
    Account``. This stub leaves out 26 MB of transitive deps.
    """
    import types

    if "eth_account" in sys.modules:
        return

    mod = types.ModuleType("eth_account")
    mod.Account = None  # type: ignore[attr-defined]
    msgs = types.ModuleType("eth_account.messages")
    msgs.encode_defunct = None  # type: ignore[attr-defined]
    mod.messages = msgs  # type: ignore[attr-defined]

    sys.modules.setdefault("eth_account", mod)
    sys.modules.setdefault("eth_account.messages", msgs)


def _prepend_vendor():
    if os.path.isdir(VENDOR_DIR) and VENDOR_DIR not in sys.path:
        sys.path.insert(0, VENDOR_DIR)


def _install_deps():
    """pip-install deps from requirements.lock into .vendor/pyX.Y/."""
    if not os.path.isfile(LOCKFILE):
        print(
            json.dumps(
                {
                    "error": "requirements.lock is missing from the skill folder",
                }
            )
        )
        sys.exit(1)
    os.makedirs(VENDOR_DIR, exist_ok=True)

    subprocess.run(
        [
            sys.executable, "-m", "pip", "install",
            "--target", VENDOR_DIR,
            "--quiet",
            "--disable-pip-version-check",
            "--only-binary=:all:",
            "--no-binary=lighter-sdk",
            "--no-deps",
            "-r", LOCKFILE,
        ],
        check=True,
        capture_output=True,
    )


def ensure_lighter():
    """Make `import lighter` work, pip-installing from requirements.lock on
    first run if needed. Everything lands in .vendor/pyX.Y/.

    On failure, prints a JSON error envelope to stdout and exits 1 so
    callers never see a Python traceback leak into the agent's context.
    """
    if sys.version_info < MIN_PYTHON:
        have = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        need = f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}"
        print(
            json.dumps(
                {
                    "error": f"lighter-agent-kit requires Python {need}+, found {have}",
                }
            )
        )
        sys.exit(1)

    _stub_eth_account()
    _prepend_vendor()
    try:
        import lighter  # noqa: F401

        return
    except ImportError:
        pass

    try:
        _install_deps()
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or b"").decode(errors="replace").strip()[-400:]
        print(
            json.dumps(
                {
                    "error": "dependency install failed",
                    "detail": detail,
                }
            )
        )
        sys.exit(1)
    except PermissionError as e:
        print(
            json.dumps(
                {
                    "error": "cannot write to skill vendor directory",
                    "detail": str(e),
                }
            )
        )
        sys.exit(1)

    _prepend_vendor()
    try:
        import lighter  # noqa: F401
    except ImportError as e:
        print(
            json.dumps(
                {
                    "error": "deps installed but still cannot import lighter",
                    "detail": str(e),
                }
            )
        )
        sys.exit(1)
