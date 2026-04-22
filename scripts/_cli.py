"""Shared CLI helpers for lighter-agent-kit scripts."""

import argparse
import json
from typing import NoReturn


def output(data):
    print(json.dumps(data, indent=2))


def error(msg) -> NoReturn:
    output({"error": msg})
    raise SystemExit(1)


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        error(message)
