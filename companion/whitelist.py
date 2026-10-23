"""群白名单。"""

from __future__ import annotations

import re

_SPLIT = re.compile(r'[\s,，;；]+')


def parse_whitelist(raw: str | None) -> frozenset[str]:
    """把 "123, 456，789" 这样的配置解析成群号集合；非数字项忽略。"""
    if not raw:
        return frozenset()
    return frozenset(item for item in _SPLIT.split(str(raw)) if item.isdigit())


def is_allowed(group_id: object, whitelist: frozenset[str]) -> bool:
    return str(group_id) in whitelist
