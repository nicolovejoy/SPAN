#!/usr/bin/env python3
"""Circuit-name → category classifier. Single source of truth: categories.json."""

import json
import re
from functools import lru_cache
from pathlib import Path

_CONFIG_PATH = Path(__file__).parent / "categories.json"


@lru_cache(maxsize=1)
def _load() -> tuple[list[tuple[str, re.Pattern]], str]:
    with _CONFIG_PATH.open() as f:
        cfg = json.load(f)
    rules = [(r["category"], re.compile(r["pattern"], re.IGNORECASE)) for r in cfg["rules"]]
    return rules, cfg.get("default", "Other")


def categorize(name: str) -> str:
    rules, default = _load()
    for category, pattern in rules:
        if pattern.search(name):
            return category
    return default
