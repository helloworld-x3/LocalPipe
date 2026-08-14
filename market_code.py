"""Shared guard for market codes that are interpolated into filesystem paths."""

from __future__ import annotations

import re

_MARKET_CODE_RE = re.compile(r"^[a-z0-9_-]+$")


def validate_market_code(market_code) -> str:
    """Normalize a market code and reject anything unsafe for path interpolation.

    Market codes feed ``f"{market}.json"`` path fragments in several modules;
    anything containing a path separator or other filesystem metacharacters
    would allow path traversal.  Accept only lowercase alphanumerics, ``-``
    and ``_``, raising ``ValueError`` otherwise.
    """
    normalized = str(market_code or "").strip().lower()
    if not _MARKET_CODE_RE.fullmatch(normalized):
        raise ValueError(f"非法 market_code（含路径分隔符或非法字符）: {market_code!r}")
    return normalized
