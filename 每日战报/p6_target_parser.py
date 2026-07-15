"""Shared parser for rendered P6 target rows."""

from __future__ import annotations

import re


P6_TARGET_LINE_PATTERN = re.compile(
    r"^\s*-\s*(?P<name>.+?)\s*[（(]\s*(?P<code>\d{6})\s*[）)]\s*\|",
    re.M,
)


def extract_p6_targets(report: str) -> list[tuple[str, str]]:
    """Return unique ``(name, code)`` pairs in P6 render order."""
    targets: dict[str, str] = {}
    for match in P6_TARGET_LINE_PATTERN.finditer(report or ""):
        name = match.group("name").strip().strip("*` ")
        code = match.group("code")
        if name and code not in targets:
            targets[code] = name
    return [(name, code) for code, name in targets.items()]
