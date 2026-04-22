#!/usr/bin/env python3
"""Diagnostic: verify the lighter SDK loads, self-installing if needed.

You don't need to run this as a separate step — `query.py` and `trade.py`
both self-heal the SDK install on first call via `scripts/_sdk.py`. This
script exists so an agent (or a human) can sanity-check the skill without
touching the API: "does `import lighter` work from the vendored directory,
and what version?"

Exit 0 on success with
    {"status": "ok", "sdk_version": "1.x.y", "vendor_dir": "..."}

Exit 1 on failure with the JSON error envelope from _sdk.ensure_lighter().
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _sdk import ensure_lighter, VENDOR_DIR  # noqa: E402

ensure_lighter()
import lighter  # noqa: E402


def main():
    print(
        json.dumps(
            {
                "status": "ok",
                "sdk_version": getattr(lighter, "__version__", "unknown"),
                "vendor_dir": VENDOR_DIR,
            }
        )
    )


if __name__ == "__main__":
    main()
