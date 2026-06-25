"""Scrub non-deterministic data from test output for stable snapshot comparison."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Rule:
    pattern: re.Pattern
    replacement: str


_RULES: list[Rule] = [
    Rule(re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"), "<UTC_TIMESTAMP>"),
    Rule(re.compile(r"\b[0-9A-F]{8}\b"), "<TRACKING_REF>"),
    Rule(re.compile(r"\b\d+\s*мин\b"), "N мин"),
]


def scrub(text: str) -> str:
    for rule in _RULES:
        text = rule.pattern.sub(rule.replacement, text)
    return text
