"""Per-user settings: profile (eligibility), filters, email preferences."""

from __future__ import annotations

import json
import sqlite3

from .config import Settings
from .filters import FilterConfig


def user_config(user: sqlite3.Row | None, settings: Settings) -> FilterConfig:
    """A user's FilterConfig (their filters + profile). Anonymous visitors get the site defaults."""
    if user is None:
        return settings.filters
    filters = {**settings.filters.to_dict(), **json.loads(user["filters"] or "{}")}
    return FilterConfig.from_dict(filters, json.loads(user["profile"] or "{}"))


def profile_complete(user: sqlite3.Row) -> bool:
    p = json.loads(user["profile"] or "{}")
    return bool(p.get("graduation") and p.get("degree_level"))
