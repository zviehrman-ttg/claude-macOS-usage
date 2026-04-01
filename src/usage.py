"""Usage data fetching from claude.ai and Claude CLI stats."""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import requests

from .auth import _session_headers
from .config import CLI_STATS_PATH, CLAUDE_AI_API


def fetch_claude_ai_usage(session_key, org_id):
    """Fetch live usage data from claude.ai internal API.

    The API returns:
    {
        "five_hour": {"utilization": 42.0, "resets_at": "2026-03-06T19:00:00+00:00"},
        "seven_day": {"utilization": 13.0, "resets_at": "..."},
        "seven_day_sonnet": {"utilization": 2.0, "resets_at": "..."},
        "extra_usage": {"is_enabled": true, ...},
        ...
    }
    """
    headers = _session_headers(session_key)
    try:
        resp = requests.get(
            f"{CLAUDE_AI_API}/organizations/{org_id}/usage",
            headers=headers,
            timeout=15,
            verify=True,
        )
        if resp.status_code == 200:
            return _parse_usage_response(resp.json())
        if resp.status_code in (401, 403):
            # Session expired or revoked
            return {"expired": True}
    except requests.RequestException:
        pass
    return None


def _parse_usage_response(data):
    """Parse the claude.ai /usage API response.

    Supports two plan modes:
    - "token_cap": Pro/Max plans with hourly/weekly utilization percentages
    - "spend_cap": Enterprise/budget plans with dollar spend limits
    """
    result = {
        "plan_mode": "token_cap",  # default; overridden to "spend_cap" if detected
        "session": {"percent": 0, "reset_at": "", "label": "Current session"},
        "weekly_all": {"percent": 0, "reset_at": "", "label": "Current week (all models)"},
        "weekly_sonnet": {"percent": 0, "reset_at": "", "label": "Current week (Sonnet only)"},
        "spend": None,       # populated for spend_cap plans
        "extra_usage": None,
    }

    if not isinstance(data, dict):
        return result

    # --- Token-cap plan fields (Pro / Max) ---
    fh = data.get("five_hour")
    if isinstance(fh, dict):
        result["session"]["percent"] = int(fh.get("utilization", 0))
        result["session"]["reset_at"] = _format_reset_time(fh.get("resets_at"))
        result["session"]["resets_at_iso"] = fh.get("resets_at", "")

    sd = data.get("seven_day")
    if isinstance(sd, dict):
        result["weekly_all"]["percent"] = int(sd.get("utilization", 0))
        result["weekly_all"]["reset_at"] = _format_reset_time(sd.get("resets_at"))
        result["weekly_all"]["resets_at_iso"] = sd.get("resets_at", "")

    sds = data.get("seven_day_sonnet")
    if isinstance(sds, dict):
        result["weekly_sonnet"]["percent"] = int(sds.get("utilization", 0))
        result["weekly_sonnet"]["reset_at"] = _format_reset_time(sds.get("resets_at"))
        result["weekly_sonnet"]["resets_at_iso"] = sds.get("resets_at", "")

    has_token_data = isinstance(fh, dict) or isinstance(sd, dict) or isinstance(sds, dict)

    # --- Spend-cap plan detection ---
    # Enterprise plans use extra_usage with credit units (1 credit = $0.01).
    # When five_hour/seven_day are null, extra_usage IS the primary usage metric.
    eu = data.get("extra_usage")
    if isinstance(eu, dict) and eu.get("is_enabled"):
        used_credits = eu.get("used_credits", 0) or 0
        monthly_limit = eu.get("monthly_limit", 0) or 0
        utilization = eu.get("utilization", 0) or 0

        if not has_token_data:
            # Enterprise / contracted plan: extra_usage is the spend limit.
            # Credits are in units of $0.01 (30000 credits = $300).
            amount_dollars = used_credits / 100
            limit_dollars = monthly_limit / 100
            percent = round(utilization, 1) if utilization else (
                int(used_credits / monthly_limit * 100) if monthly_limit else 0
            )
            result["plan_mode"] = "spend_cap"
            result["spend"] = {
                "amount": amount_dollars,
                "limit": limit_dollars,
                "percent": int(percent),
                "reset_at": "",  # enterprise resets are contract-defined
            }
        else:
            # Pro/Max top-up credits alongside normal token limits
            result["extra_usage"] = {
                "enabled": True,
                "used_credits": used_credits,
                "monthly_limit": monthly_limit,
            }

    if result["plan_mode"] == "token_cap" and not has_token_data:
        logging.debug("claude.ai /usage response keys (unknown plan): %s", list(data.keys()))
        result["plan_mode"] = "unknown"

    return result


def _format_reset_time(reset_str):
    """Format a reset timestamp into a human-readable string."""
    if not reset_str:
        return ""
    try:
        if isinstance(reset_str, (int, float)):
            dt = datetime.fromtimestamp(reset_str / 1000 if reset_str > 1e12 else reset_str)
        else:
            dt = datetime.fromisoformat(str(reset_str).replace("Z", "+00:00"))
            dt = dt.astimezone()
        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        diff = dt - now
        if diff.total_seconds() < 0:
            return "now"
        if diff.days > 0:
            return dt.strftime("%a %b %d %I:%M %p")
        hours = diff.seconds // 3600
        mins = (diff.seconds % 3600) // 60
        if hours > 0:
            return f"in {hours}h {mins}m"
        return f"in {mins}m"
    except (ValueError, TypeError, OSError):
        return str(reset_str)


def get_cli_stats():
    """Read usage stats from Claude CLI's stats-cache.json.

    Returns summary of today's and this week's activity.
    """
    if not os.path.exists(CLI_STATS_PATH):
        return None

    try:
        with open(CLI_STATS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    today = datetime.now().strftime("%Y-%m-%d")
    week_start = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime("%Y-%m-%d")

    daily_activity = data.get("dailyActivity", [])
    daily_tokens = data.get("dailyModelTokens", [])
    model_usage = data.get("modelUsage", {})

    # Today's stats
    today_msgs = 0
    today_sessions = 0
    today_tools = 0
    week_msgs = 0
    week_sessions = 0

    for entry in daily_activity:
        if entry.get("date") == today:
            today_msgs = entry.get("messageCount", 0)
            today_sessions = entry.get("sessionCount", 0)
            today_tools = entry.get("toolCallCount", 0)
        if entry.get("date", "") >= week_start:
            week_msgs += entry.get("messageCount", 0)
            week_sessions += entry.get("sessionCount", 0)

    # Today's tokens by model
    today_tokens_by_model = {}
    week_tokens_by_model = {}
    for entry in daily_tokens:
        if entry.get("date") == today:
            today_tokens_by_model = entry.get("tokensByModel", {})
        if entry.get("date", "") >= week_start:
            for model, count in entry.get("tokensByModel", {}).items():
                week_tokens_by_model[model] = week_tokens_by_model.get(model, 0) + count

    return {
        "today_messages": today_msgs,
        "today_sessions": today_sessions,
        "today_tools": today_tools,
        "week_messages": week_msgs,
        "week_sessions": week_sessions,
        "today_tokens_by_model": today_tokens_by_model,
        "week_tokens_by_model": week_tokens_by_model,
        "total_messages": data.get("totalMessages", 0),
        "total_sessions": data.get("totalSessions", 0),
        "model_usage": model_usage,
    }


def get_reset_countdown():
    """Calculate time until daily and weekly resets."""
    now = datetime.now()
    daily_reset = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0)
    daily_diff = daily_reset - now
    daily_hours = daily_diff.seconds // 3600
    daily_mins = (daily_diff.seconds % 3600) // 60

    days_until_monday = (7 - now.weekday()) % 7 or 7
    weekly_reset = (now + timedelta(days=days_until_monday)).replace(hour=0, minute=0, second=0)
    weekly_diff = weekly_reset - now

    return {
        "daily": f"{daily_hours}h {daily_mins}m",
        "weekly": f"{weekly_diff.days}d {weekly_diff.seconds // 3600}h",
    }


def format_tokens(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def build_bar(percent, width=20):
    """Build a text progress bar from a percentage."""
    percent = max(0, min(100, percent))
    filled = int(percent / 100 * width)
    bar = "\u2588" * filled + "\u2591" * (width - filled)
    warning = " !!!" if percent > 80 else ""
    return f"[{bar}]{warning}"


def shorten_model_name(model_id):
    """Shorten model IDs for display."""
    mapping = {
        "claude-opus-4-6": "Opus 4.6",
        "claude-sonnet-4-6": "Sonnet 4.6",
        "claude-sonnet-4-5-20250929": "Sonnet 4.5",
        "claude-haiku-4-5-20251001": "Haiku 4.5",
    }
    return mapping.get(model_id, model_id.split("/")[-1])




def get_claude_code_token_stats():
    """Parse ~/.claude/projects/**/*.jsonl for token usage today and this week."""
    import glob
    from datetime import datetime, timedelta

    week_ago = (datetime.now() - timedelta(days=7)).isoformat()
    today = datetime.now().strftime("%Y-%m-%d")

    today_tokens = {}
    week_tokens = {}

    for f in glob.glob(os.path.expanduser("~/.claude/projects/**/*.jsonl"), recursive=True):
        try:
            for line in open(f, errors="ignore"):
                entry = json.loads(line)
                ts = entry.get("timestamp", "")
                if not ts or ts < week_ago:
                    continue
                usage = (
                    entry.get("usage")
                    or entry.get("message", {}).get("usage")
                    or {}
                )
                inp = usage.get("input_tokens", 0) or 0
                out = usage.get("output_tokens", 0) or 0
                total = inp + out
                if not total:
                    continue
                model = (
                    entry.get("message", {}).get("model")
                    or entry.get("model")
                    or "unknown"
                )
                week_tokens[model] = week_tokens.get(model, 0) + total
                if ts[:10] == today:
                    today_tokens[model] = today_tokens.get(model, 0) + total
        except Exception:
            continue

    if not week_tokens:
        return None
    return {
        "today_tokens_by_model": today_tokens,
        "week_tokens_by_model": week_tokens,
    }


def predict_pace(used_pct: float, resets_at_iso: str | None, window_hours: int = 168) -> str:
    """Return a pace prediction string: on track / runs out early / underutilising."""
    if used_pct is None:
        return ""
    try:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        total_secs = window_hours * 3600
        if resets_at_iso and resets_at_iso.strip():
            resets = datetime.fromisoformat(resets_at_iso)
            if resets.tzinfo is None:
                resets = resets.replace(tzinfo=timezone.utc)
            elapsed_secs = total_secs - (resets - now).total_seconds()
        else:
            # No reset time — assume we're halfway through the window
            elapsed_secs = total_secs / 2
        if elapsed_secs <= 0:
            elapsed_secs = total_secs * 0.01  # treat as 1% elapsed to avoid divide-by-zero
        elapsed_pct = min(elapsed_secs / total_secs * 100, 100)
        # At this pace, projected % at end of window
        if elapsed_pct == 0:
            return ""
        projected = (used_pct / elapsed_pct) * 100
        ratio = used_pct / max(elapsed_pct, 1)

        days_remaining = (resets - now).total_seconds() / 86400
        if projected >= 100:
            # Will run out before reset
            secs_until_empty = (total_secs * elapsed_secs / max(used_pct, 0.01)) - elapsed_secs
            # Alternatively: at current burn rate, time to 100%
            burn_rate = used_pct / elapsed_secs  # % per second
            if burn_rate > 0:
                secs_to_full = (100 - used_pct) / burn_rate
                days_to_full = secs_to_full / 86400
                if days_to_full < 1:
                    hrs = int(secs_to_full / 3600)
                    return f"    ⚠️  Runs out in ~{hrs}h"
                else:
                    return f"    ⚠️  Runs out in ~{days_to_full:.1f}d"
            return "    ⚠️  On pace to exceed limit"
        elif ratio > 1.3:
            return f"    📈  High usage — projected {int(projected)}% by reset"
        elif ratio < 0.5:
            return f"    💤  Underutilising ({int(projected)}% projected)"
        else:
            return f"    ✅  On track ({int(projected)}% projected)"
    except Exception:
        return ""
