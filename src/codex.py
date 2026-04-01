"""Codex (ChatGPT) usage tracking.

Two data sources:
  1. ~/.codex/auth.json   — OAuth tokens to call chatgpt.com/backend-api/wham/usage
  2. ~/.codex/state_5.sqlite — local thread history for token counts

Returns None gracefully if Codex is not installed / auth missing.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import os

import requests


def _real_home() -> Path:
    """Get the real user home, not sandbox-overridden Path.home()."""
    env_home = os.environ.get("HOME", "")
    if env_home and Path(env_home).exists():
        return Path(env_home)
    return Path.home()


_AUTH_PATH = _real_home() / ".codex" / "auth.json"
_DB_PATH = _real_home() / ".codex" / "state_5.sqlite"

_WHAM_URL = "https://chatgpt.com/backend-api/wham/usage"

_PLAN_NAMES = {
    "free": "Free",
    "go": "Go",
    "plus": "Plus",
    "pro": "Pro",
    "team": "Team",
    "business": "Business",
    "enterprise": "Enterprise",
    "education": "Education",
    "edu": "Edu",
    "guest": "Guest",
}


def _load_auth() -> dict | None:
    """Read ~/.codex/auth.json and return token info, or None."""
    if not _AUTH_PATH.exists():
        return None
    try:
        data = json.loads(_AUTH_PATH.read_text())
        tokens = data.get("tokens", {})
        access_token = tokens.get("access_token")
        if not access_token:
            return None
        return {
            "access_token": access_token,
            "account_id": data.get("account_id") or tokens.get("account_id"),
        }
    except (json.JSONDecodeError, OSError):
        return None


def get_codex_live_usage() -> dict | None:
    """Fetch live rate-limit usage from chatgpt.com/backend-api/wham/usage.

    Returns dict with plan info and rate limit windows, or None on failure.
    """
    auth = _load_auth()
    if not auth:
        return None

    try:
        resp = requests.get(
            _WHAM_URL,
            headers={
                "Authorization": f"Bearer {auth['access_token']}",
                "Accept": "application/json",
                "User-Agent": "claude-macOS-usage/1.0",
            },
            timeout=10,
        )
        if resp.status_code != 200:
            return None

        data = resp.json()
        rate = data.get("rate_limit", {}) or {}
        primary = rate.get("primary_window") or {}
        secondary = rate.get("secondary_window") or {}
        plan_type = data.get("plan_type", "unknown")

        return {
            "plan_type": plan_type,
            "plan_name": _PLAN_NAMES.get(plan_type, plan_type.title()),
            "email": data.get("email", ""),
            "limit_reached": rate.get("limit_reached", False),
            "primary_pct": primary.get("used_percent", 0),
            "primary_window_h": round(primary.get("limit_window_seconds", 0) / 3600, 1),
            "primary_reset_s": primary.get("reset_after_seconds", 0),
            "primary_resets_at": (
                (__import__("datetime").datetime.now(__import__("datetime").timezone.utc) +
                 __import__("datetime").timedelta(seconds=primary.get("reset_after_seconds", 0))).isoformat()
                if primary.get("reset_after_seconds") else None
            ),
            "secondary_reset_s": secondary.get("reset_after_seconds", 0),
            "secondary_resets_at": (
                (__import__("datetime").datetime.now(__import__("datetime").timezone.utc) +
                 __import__("datetime").timedelta(seconds=secondary.get("reset_after_seconds", 0))).isoformat()
                if secondary.get("reset_after_seconds") else None
            ),
            "secondary_pct": secondary.get("used_percent", 0),
            "secondary_window_h": round(secondary.get("limit_window_seconds", 0) / 3600, 1),
            "secondary_reset_s": secondary.get("reset_after_seconds", 0),
        }
    except Exception:
        return None


def get_codex_local_stats() -> dict | None:
    """Read token usage from ~/.codex/state_5.sqlite.

    Returns dict with today/week token totals by model, or None.
    """
    if not _DB_PATH.exists():
        return None

    try:
        con = sqlite3.connect(f"file:{_DB_PATH}?mode=ro", uri=True, timeout=3)
        cur = con.cursor()

        # Detect ms vs s timestamps
        cur.execute("SELECT MAX(created_at) FROM threads")
        row = cur.fetchone()
        if not row or not row[0]:
            con.close()
            return None

        max_ts = row[0]
        scale = 1000 if max_ts > 1_000_000_000_000 else 1

        now_s = int(datetime.now(timezone.utc).timestamp())
        today_start_s = int(
            datetime.now(timezone.utc)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )

        cur.execute("""
            SELECT COALESCE(model, 'unknown'), SUM(tokens_used)
            FROM threads
            WHERE created_at >= ?
            GROUP BY model
            ORDER BY SUM(tokens_used) DESC
        """, (today_start_s * scale,))
        today_by_model = {r[0]: r[1] for r in cur.fetchall() if r[1]}

        cur.execute("""
            SELECT COALESCE(model, 'unknown'), SUM(tokens_used)
            FROM threads
            WHERE created_at >= ?
            GROUP BY model
            ORDER BY SUM(tokens_used) DESC
        """, ((now_s - 7 * 86400) * scale,))
        week_by_model = {r[0]: r[1] for r in cur.fetchall() if r[1]}

        con.close()
        return {
            "today_tokens_by_model": today_by_model,
            "week_tokens_by_model": week_by_model,
            "today_total": sum(today_by_model.values()),
            "week_total": sum(week_by_model.values()),
        }
    except Exception:
        return None


def get_codex_stats() -> dict | None:
    """Combined Codex stats: presence check only (live fetched separately).

    Returns a sentinel dict if auth exists, else None.
    """
    auth = _load_auth()
    local = get_codex_local_stats()
    if not auth and not local:
        return None

    result = {
        "live": None,
        "today_tokens_by_model": {},
        "week_tokens_by_model": {},
        "today_total": 0,
        "week_total": 0,
    }
    if local:
        result.update(local)
    return result


def format_reset_time(seconds: int) -> str:
    """Format seconds-until-reset as duration plus absolute local time."""
    if seconds <= 0:
        return "now"

    total_minutes = max(1, (seconds + 59) // 60)
    days, rem_minutes = divmod(total_minutes, 60 * 24)
    hours, minutes = divmod(rem_minutes, 60)

    if days > 0:
        duration = f"{days} day{'s' if days != 1 else ''}, {hours} hour{'s' if hours != 1 else ''}"
    elif hours > 0:
        duration = f"{hours} hour{'s' if hours != 1 else ''}, {minutes} minute{'s' if minutes != 1 else ''}"
    else:
        duration = f"{minutes} minute{'s' if minutes != 1 else ''}"

    reset_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    local = reset_at.astimezone()
    clock = local.strftime("%I:%M %p").lstrip("0")
    absolute = f"{local.strftime('%a %b')} {local.day}, {clock}"
    return f"{duration} ({absolute})"
