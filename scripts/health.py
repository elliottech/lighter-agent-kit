#!/usr/bin/env python3
"""Health check for the Lighter Agent Kit."""

import json

print(json.dumps({"status": "ok", "version": "0.1.0"}))
