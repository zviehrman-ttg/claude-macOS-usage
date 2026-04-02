#!/usr/bin/env python3
"""Backend for Claude Usage Monitor — called by Swift host app.

Usage:
    python3 backend.py init
    python3 backend.py refresh [org_id]
    python3 backend.py connect-chrome
    python3 backend.py connect-manual <session_key>
    python3 backend.py disconnect
    python3 backend.py switch-org <org_id>

All commands print a JSON object to stdout.
"""

import json
import sys

from src.auth import (
    fetch_claude_oauth_usage,
    delete_preferred_org,
    delete_session_key,
    detect_tier_from_cli,
    extract_chrome_session_key,
    get_chat_organizations,
    get_cli_credentials,
    get_cli_username,
    get_preferred_org,
    get_session_cookie_instructions,
    get_session_key,
    has_cli_credentials,
    save_preferred_org,
    save_session_key,
)
from src.config import TIER_MAP, TIERS
from src.usage import (
    build_bar,
    format_reset_time as usage_format_reset,
    predict_pace,
    fetch_claude_ai_usage,
    format_tokens,
    get_cli_stats,
    get_claude_code_token_stats,
    get_reset_countdown,
    shorten_model_name,
)
from src.codex import get_codex_stats, get_codex_live_usage, format_reset_time as codex_format_reset


def _select_org(chat_orgs, preferred_id=None):
    if preferred_id:
        for org in chat_orgs:
            if org["org_id"] == preferred_id:
                return org
    return chat_orgs[0] if chat_orgs else None


def _format_codex_identity(codex_live):
    """Build a concise Codex identity label for the top header."""
    if not codex_live:
        return ""

    plan = str(codex_live.get("plan_label") or codex_live.get("plan_name", "") or "").strip()
    email = str(codex_live.get("email", "") or "").strip()
    account = email.split("@", 1)[0] if "@" in email else email
    if len(account) > 18:
        account = account[:15] + "..."

    if account and plan:
        return f"{account} - {plan}"
    if plan:
        return f"Codex {plan}"
    if account:
        return account
    return "Codex"


def _claude_summary_label(has_session, live_usage, claude_oauth_usage):
    """Compact top-level summary label for Claude submenu."""
    pct = None
    suffix = "no live data"
    if live_usage:
        plan_mode = live_usage.get("plan_mode", "token_cap")
        if plan_mode == "spend_cap" and live_usage.get("spend"):
            pct = live_usage["spend"]["percent"]
            suffix = f"{pct}% spend"
        elif plan_mode == "token_cap":
            pct = live_usage["session"]["percent"]
            suffix = f"{pct}%"
        else:
            suffix = "usage unavailable"
    elif has_session:
        suffix = "loading..."
    elif claude_oauth_usage:
        fh = claude_oauth_usage.get("five_hour") or {}
        if fh:
            pct = int(fh.get("utilization", 0))
            suffix = f"{pct}%"

    if pct is None:
        dot = "\U0001F535"  # blue
    elif pct > 80:
        dot = "\U0001F7E0"  # orange
    elif pct > 50:
        dot = "\U0001F7E1"  # yellow
    else:
        dot = "\U0001F7E2"  # green
    return f"{dot} Claude \u00B7 {suffix}"


def _codex_summary_label(codex_live, codex_stats):
    """Compact top-level summary label for Codex submenu."""
    if codex_live:
        pct = codex_live.get("primary_pct")
        if codex_live.get("limit_reached"):
            dot = "\U0001F534"  # red
        elif pct > 80:
            dot = "\U0001F7E0"  # orange
        elif pct > 50:
            dot = "\U0001F7E1"  # yellow
        else:
            dot = "\U0001F7E2"  # green
        return f"{dot} Codex \u00B7 {pct}%"
    if codex_stats:
        return "\U0001F535 Codex \u00B7 local stats only"
    return "\u26AA Codex \u00B7 not connected"


def _trim_edge_separators(items):
    """Remove leading/trailing separators from a menu item list."""
    trimmed = list(items)
    while trimmed and trimmed[0].get("type") == "separator":
        trimmed = trimmed[1:]
    while trimmed and trimmed[-1].get("type") == "separator":
        trimmed = trimmed[:-1]
    return trimmed


def _compact_provider_menu(menu_items, has_session, live_usage, claude_oauth_usage, codex_live, codex_stats):
    """Convert flat Claude/Codex sections into compact provider submenus."""
    if len(menu_items) < 2:
        return menu_items

    claude_idx = -1
    codex_idx = -1
    for i, item in enumerate(menu_items):
        if item.get("type") != "item":
            continue
        title = item.get("title") or ""
        if title.startswith("── Claude"):
            claude_idx = i
        elif title.startswith("── Codex"):
            codex_idx = i

    if claude_idx == -1:
        return menu_items

    prelude = menu_items[:2]  # account header + separator
    claude_end = codex_idx if codex_idx != -1 else len(menu_items)
    claude_items = _trim_edge_separators(menu_items[claude_idx + 1:claude_end])

    codex_items = []
    if codex_idx != -1:
        codex_end = len(menu_items) - 1 if menu_items and menu_items[-1].get("type") == "separator" else len(menu_items)
        codex_items = _trim_edge_separators(menu_items[codex_idx + 1:codex_end])

    if not claude_items:
        claude_items = [{"type": "item", "title": "  No Claude data yet"}]
    if not codex_items:
        codex_items = [{"type": "item", "title": "  No Codex data yet"}]

    compact = list(prelude)
    compact.append({
        "type": "submenu",
        "title": _claude_summary_label(has_session, live_usage, claude_oauth_usage),
        "children": claude_items,
    })
    compact.append({
        "type": "submenu",
        "title": _codex_summary_label(codex_live, codex_stats),
        "children": codex_items,
    })
    compact.append({"type": "separator"})
    return compact


def _build_state(tier="pro", username=None, org_id=None, has_session=False,
                 has_cli_creds=False, available_orgs=None, live_usage=None,
                 claude_oauth_usage=None, cli_stats=None, claude_code_stats=None,
                 codex_stats=None, codex_live=None,
                 message=None, error=None):
    """Build the full state dict returned to Swift."""
    available_orgs = available_orgs or []
    tier_info = TIERS.get(tier, TIERS["pro"])
    resets = get_reset_countdown()

    menu_items = []

    # Header
    price = tier_info["price"]
    tier_label = tier_info["name"] if price == tier_info["name"] else f"{tier_info['name']} ({price})"
    header = f"{username} - {tier_label}" if username else tier_label
    codex_identity = _format_codex_identity(codex_live)
    if codex_identity:
        header = f"{header} / {codex_identity}"
    menu_items.append({"type": "item", "title": header})
    menu_items.append({"type": "separator"})

    # ---- Claude Usage header ----
    menu_items.append({"type": "item", "title": "\u2500\u2500 Claude \u2500\u2500"})

    # ---- Live usage (from claude.ai session) ----
    if live_usage:
        plan_mode = live_usage.get("plan_mode", "token_cap")

        if plan_mode == "spend_cap":
            spend = live_usage["spend"]
            pct = spend["percent"]
            menu_items.append({"type": "item", "title": "  Monthly spend"})
            menu_items.append({"type": "item", "title": f"    {build_bar(pct)}  {pct}% used"})
            menu_items.append({"type": "item", "title": f"    ${spend['amount']:.2f} of ${spend['limit']:.2f} spent"})
            if spend["reset_at"]:
                menu_items.append({"type": "item", "title": f"    Resets {spend['reset_at']}"})
            menu_items.append({"type": "separator"})

        elif plan_mode == "unknown":
            menu_items.append({"type": "item", "title": "  Usage data unavailable for this plan"})
            menu_items.append({"type": "item", "title": "  See claude.ai/settings/usage"})
            menu_items.append({"type": "separator"})

        else:
            for key in ("session", "weekly_all", "weekly_sonnet"):
                bucket = live_usage[key]
                pct = bucket["percent"]
                resets_at_iso = bucket.get("resets_at_iso", "")
                window_h = 5 if key == "session" else 168
                pace = predict_pace(pct, resets_at_iso, window_h)

                menu_items.append({"type": "item", "title": f"  {bucket['label']}"})
                menu_items.append({"type": "item", "title": f"    {build_bar(pct)}  {pct}% used"})
                if pace:
                    menu_items.append({"type": "item", "title": f"{pace}  [{key}]"})
                if bucket["reset_at"]:
                    menu_items.append({"type": "item", "title": f"    Resets {bucket['reset_at']}"})
                menu_items.append({"type": "separator"})

    elif has_session:
        menu_items.append({"type": "item", "title": "  Loading live usage..."})
        menu_items.append({"type": "separator"})

    elif claude_oauth_usage:
        # OAuth usage from CLI token (no session cookie needed)
        ou = claude_oauth_usage
        buckets = [
            ("five_hour",        "  5h session"),
            ("seven_day",        "  7-day (all models)"),
            ("seven_day_opus",   "  7-day Opus"),
            ("seven_day_sonnet", "  7-day Sonnet"),
        ]
        for key, label in buckets:
            bucket = ou.get(key) or {}
            if not bucket:
                continue
            pct = bucket.get("utilization", 0)
            resets_at = bucket.get("resets_at", "")
            window_h = 5 if key == "five_hour" else 168
            pace = predict_pace(pct, resets_at, window_h)
            menu_items.append({"type": "item", "title": label})
            menu_items.append({"type": "item", "title": f"    {build_bar(int(pct))}  {int(pct)}% used"})
            if pace:
                menu_items.append({"type": "item", "title": f"{pace}  [{key}]"})
            if resets_at:
                menu_items.append({"type": "item", "title": f"    Resets {usage_format_reset(resets_at)}"})
            menu_items.append({"type": "separator"})

        # Extra (add-on) credits
        extra = ou.get("extra_usage") or {}
        if extra.get("is_enabled"):
            used = int(extra.get("used_credits", 0))
            limit = int(extra.get("monthly_limit", 0))
            pct = min(int(extra.get("utilization", 0)), 100)
            warn = "  \u26a0\ufe0f Add-on credits" if pct >= 100 else "  Add-on credits"
            menu_items.append({"type": "item", "title": warn})
            menu_items.append({"type": "item", "title": f"    {build_bar(pct)}  {used}/{limit} used ({pct}%)"})
            menu_items.append({"type": "separator"})
    else:
        menu_items.append({"type": "item", "title": "  Usage limits (connect for live data)"})
        menu_items.append({"type": "separator"})
        menu_items.append({"type": "item", "title": f"    Daily resets in:  {resets['daily']}"})
        menu_items.append({"type": "item", "title": f"    Weekly resets in: {resets['weekly']}"})
        menu_items.append({"type": "separator"})

    # ---- Claude Code Token Stats ----
    token_stats = claude_code_stats or cli_stats
    if token_stats:
        if token_stats.get("today_tokens_by_model"):
            menu_items.append({"type": "separator"})
            menu_items.append({"type": "item", "title": "    Tokens today:"})
            for model, count in sorted(token_stats["today_tokens_by_model"].items(), key=lambda x: -x[1]):
                name = shorten_model_name(model)
                menu_items.append({"type": "item", "title": f"      {name}: {format_tokens(count)}"})

        if token_stats.get("week_tokens_by_model"):
            menu_items.append({"type": "separator"})
            menu_items.append({"type": "item", "title": "    Tokens this week:"})
            for model, count in sorted(token_stats["week_tokens_by_model"].items(), key=lambda x: -x[1]):
                name = shorten_model_name(model)
                menu_items.append({"type": "item", "title": f"      {name}: {format_tokens(count)}"})

    # ---- Codex Usage ----
    if codex_stats or codex_live:
        menu_items.append({"type": "separator"})

        if codex_live:
            codex_plan = codex_live.get("plan_label") or codex_live.get("plan_name", "Unknown")
            header = f"\u2500\u2500 Codex ({codex_plan}) \u2500\u2500"
        else:
            header = "\u2500\u2500 Codex \u2500\u2500"
        menu_items.append({"type": "item", "title": header})

        if codex_live:
            p_pct = codex_live["primary_pct"]
            p_h = codex_live["primary_window_h"]
            p_reset = codex_format_reset(codex_live["primary_reset_s"])
            p_pace = predict_pace(p_pct, codex_live.get("primary_resets_at"), p_h)
            menu_items.append({"type": "item", "title": f"  {build_bar(p_pct)}  {p_pct}% used ({p_h}h window)"})
            if p_pace:
                menu_items.append({"type": "item", "title": p_pace})
            menu_items.append({"type": "item", "title": f"    Resets in {p_reset}"})

            s_pct = codex_live["secondary_pct"]
            if s_pct > 0 or codex_live["secondary_window_h"] > 0:
                s_h = codex_live["secondary_window_h"]
                s_reset = codex_format_reset(codex_live["secondary_reset_s"])
                s_pace = predict_pace(s_pct, codex_live.get("secondary_resets_at"), s_h)
                menu_items.append({"type": "item", "title": f"  {build_bar(s_pct)}  {s_pct}% used ({s_h}h window)"})
                if s_pace:
                    menu_items.append({"type": "item", "title": s_pace})
                menu_items.append({"type": "item", "title": f"    Resets in {s_reset}"})
        else:
            menu_items.append({"type": "item", "title": "  (not connected)"})

        if codex_stats:
            if codex_stats.get("today_tokens_by_model"):
                menu_items.append({"type": "separator"})
                menu_items.append({"type": "item", "title": "    Tokens today:"})
                for model, count in codex_stats["today_tokens_by_model"].items():
                    menu_items.append({"type": "item", "title": f"      {model}: {format_tokens(count)}"})
            if codex_stats.get("week_tokens_by_model"):
                menu_items.append({"type": "separator"})
                menu_items.append({"type": "item", "title": "    Tokens this week:"})
                for model, count in codex_stats["week_tokens_by_model"].items():
                    menu_items.append({"type": "item", "title": f"      {model}: {format_tokens(count)}"})

    menu_items.append({"type": "separator"})
    menu_items = _compact_provider_menu(
        menu_items,
        has_session=has_session,
        live_usage=live_usage,
        claude_oauth_usage=claude_oauth_usage,
        codex_live=codex_live,
        codex_stats=codex_stats,
    )

    # ---- Title icon (dual dots) ----
    # Claude dot
    claude_dot = "\u2728"
    pct = None
    if live_usage:
        plan_mode = live_usage.get("plan_mode", "token_cap")
        if plan_mode == "spend_cap" and live_usage.get("spend"):
            pct = live_usage["spend"]["percent"]
        elif plan_mode == "token_cap":
            pct = live_usage["session"]["percent"]
    elif claude_oauth_usage:
        fh = claude_oauth_usage.get("five_hour") or {}
        pct = int(fh.get("utilization", 0)) if fh else None
    elif cli_stats and cli_stats.get("today_messages", 0) > 0:
        tier_daily = {"free": 25, "pro": 100, "max_5x": 500, "max_20x": 2000}
        limit = tier_daily.get(tier, 100)
        pct = min(int(cli_stats["today_messages"] / limit * 100), 100)

    if pct is not None:
        if pct > 80:
            claude_dot = "\U0001F7E0"   # Orange
        elif pct > 50:
            claude_dot = "\U0001F7E1"   # Yellow
        else:
            claude_dot = "\U0001F7E2"   # Green

    # Codex dot
    codex_dot = ""
    if codex_live:
        if codex_live.get("limit_reached"):
            codex_dot = "\U0001F534"  # Red
        elif codex_live["primary_pct"] > 80:
            codex_dot = "\U0001F7E0"  # Orange
        elif codex_live["primary_pct"] > 50:
            codex_dot = "\U0001F7E1"  # Yellow
        else:
            codex_dot = "\U0001F7E2"  # Green
    elif codex_stats:
        codex_dot = "\U0001F535"  # Blue

    if codex_dot:
        title_icon = f"{claude_dot} / {codex_dot}"
    else:
        title_icon = claude_dot

    return {
        "tier": tier,
        "username": username,
        "org_id": org_id,
        "has_session": has_session,
        "has_cli_creds": has_cli_creds,
        "available_orgs": available_orgs,
        "title_icon": title_icon,
        "menu_items": menu_items,
        "message": message,
        "error": error,
    }


def _gather_common():
    """Gather all common data sources."""
    return {
        "tier": detect_tier_from_cli() or "pro",
        "username": get_cli_username(),
        "cli_stats": get_cli_stats(),
        "claude_code_stats": get_claude_code_token_stats(),
        "codex_stats": get_codex_stats(),
        "codex_live": get_codex_live_usage(),
        "claude_oauth": fetch_claude_oauth_usage(),
        "cli_creds": has_cli_credentials(),
    }


def cmd_init():
    d = _gather_common()

    org_id = None
    has_session = False
    session_tier = None
    chat_orgs = []

    session_key = get_session_key()
    if session_key:
        chat_orgs = get_chat_organizations(session_key)

    # Try auto-extracting from Chrome if no valid session
    if not chat_orgs:
        chrome_key = extract_chrome_session_key()
        if chrome_key:
            chat_orgs = get_chat_organizations(chrome_key)
            if chat_orgs:
                save_session_key(chrome_key)

    if chat_orgs:
        has_session = True
        preferred = get_preferred_org()
        selected = _select_org(chat_orgs, preferred)
        if selected:
            org_id = selected["org_id"]
            session_tier = selected.get("rate_limit_tier")

    resolved_tier = TIER_MAP.get(session_tier) if session_tier else None
    tier = resolved_tier if resolved_tier else d["tier"]

    # Fetch live usage if we have a session
    live_usage = None
    if has_session and org_id:
        sk = get_session_key()
        if sk:
            live_usage = fetch_claude_ai_usage(sk, org_id)
            if live_usage and live_usage.get("expired"):
                delete_session_key()
                has_session = False
                org_id = None
                live_usage = None
                chat_orgs = []

    return _build_state(
        tier=tier, username=d["username"], org_id=org_id,
        has_session=has_session, has_cli_creds=d["cli_creds"],
        available_orgs=chat_orgs, live_usage=live_usage,
        claude_oauth_usage=d["claude_oauth"],
        cli_stats=d["cli_stats"], claude_code_stats=d["claude_code_stats"],
        codex_stats=d["codex_stats"], codex_live=d["codex_live"],
    )


def cmd_refresh(org_id=None):
    d = _gather_common()

    session_key = get_session_key()
    has_session = False
    live_usage = None
    chat_orgs = []

    if session_key:
        chat_orgs = get_chat_organizations(session_key)
        if chat_orgs:
            has_session = True
            if not org_id:
                preferred = get_preferred_org()
                selected = _select_org(chat_orgs, preferred)
                org_id = selected["org_id"] if selected else None

            if org_id:
                live_usage = fetch_claude_ai_usage(session_key, org_id)
                if live_usage and live_usage.get("expired"):
                    delete_session_key()
                    has_session = False
                    org_id = None
                    live_usage = None
                    chat_orgs = []

    session_tier = None
    if chat_orgs and org_id:
        for o in chat_orgs:
            if o["org_id"] == org_id:
                session_tier = o.get("rate_limit_tier")
    resolved = TIER_MAP.get(session_tier) if session_tier else None
    tier = resolved if resolved else d["tier"]

    return _build_state(
        tier=tier, username=d["username"], org_id=org_id,
        has_session=has_session, has_cli_creds=d["cli_creds"],
        available_orgs=chat_orgs, live_usage=live_usage,
        claude_oauth_usage=d["claude_oauth"],
        cli_stats=d["cli_stats"], claude_code_stats=d["claude_code_stats"],
        codex_stats=d["codex_stats"], codex_live=d["codex_live"],
    )


def cmd_connect_chrome():
    chrome_key = extract_chrome_session_key()
    if not chrome_key:
        return {"success": False, "error": "Could not extract Chrome cookie. Use manual connect."}

    chat_orgs = get_chat_organizations(chrome_key)
    if not chat_orgs:
        return {"success": False, "error": "Invalid session from Chrome. Try manual connect."}

    save_session_key(chrome_key)
    selected = _select_org(chat_orgs)
    org_id = selected["org_id"] if selected else None

    d = _gather_common()
    live_usage = None
    if org_id:
        live_usage = fetch_claude_ai_usage(chrome_key, org_id)

    tier = d["tier"]
    if selected:
        t = TIER_MAP.get(selected.get("rate_limit_tier", ""))
        if t:
            tier = t

    state = _build_state(
        tier=tier, username=d["username"], org_id=org_id,
        has_session=True, has_cli_creds=d["cli_creds"],
        available_orgs=chat_orgs, live_usage=live_usage,
        claude_oauth_usage=d["claude_oauth"],
        cli_stats=d["cli_stats"], claude_code_stats=d["claude_code_stats"],
        codex_stats=d["codex_stats"], codex_live=d["codex_live"],
        message="Connected via Chrome cookies!",
    )
    state["success"] = True
    return state


def cmd_connect_manual(session_key):
    chat_orgs = get_chat_organizations(session_key)
    if not chat_orgs:
        return {"success": False, "error": "Invalid or expired session cookie."}

    save_session_key(session_key)
    selected = _select_org(chat_orgs)
    org_id = selected["org_id"] if selected else None

    d = _gather_common()
    live_usage = None
    if org_id:
        live_usage = fetch_claude_ai_usage(session_key, org_id)

    tier = d["tier"]
    if selected:
        t = TIER_MAP.get(selected.get("rate_limit_tier", ""))
        if t:
            tier = t

    state = _build_state(
        tier=tier, username=d["username"], org_id=org_id,
        has_session=True, has_cli_creds=d["cli_creds"],
        available_orgs=chat_orgs, live_usage=live_usage,
        claude_oauth_usage=d["claude_oauth"],
        cli_stats=d["cli_stats"], claude_code_stats=d["claude_code_stats"],
        codex_stats=d["codex_stats"], codex_live=d["codex_live"],
        message="Connected!",
    )
    state["success"] = True
    return state


def cmd_disconnect():
    delete_session_key()
    delete_preferred_org()
    d = _gather_common()
    return _build_state(
        tier=d["tier"], username=d["username"],
        has_cli_creds=d["cli_creds"],
        claude_oauth_usage=d["claude_oauth"],
        cli_stats=d["cli_stats"], claude_code_stats=d["claude_code_stats"],
        codex_stats=d["codex_stats"], codex_live=d["codex_live"],
        message="Disconnected.",
    )


def cmd_switch_org(org_id):
    save_preferred_org(org_id)
    return cmd_refresh(org_id=org_id)


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "No command specified"}))
        sys.exit(1)

    cmd = sys.argv[1]
    try:
        if cmd == "init":
            result = cmd_init()
        elif cmd == "refresh":
            org_id = sys.argv[2] if len(sys.argv) > 2 else None
            result = cmd_refresh(org_id=org_id)
        elif cmd == "connect-chrome":
            result = cmd_connect_chrome()
        elif cmd == "connect-manual":
            if len(sys.argv) < 3:
                result = {"error": "Missing session key"}
            else:
                result = cmd_connect_manual(sys.argv[2])
        elif cmd == "disconnect":
            result = cmd_disconnect()
        elif cmd == "switch-org":
            if len(sys.argv) < 3:
                result = {"error": "Missing org_id"}
            else:
                result = cmd_switch_org(sys.argv[2])
        elif cmd == "instructions":
            result = {"instructions": get_session_cookie_instructions()}
        else:
            result = {"error": f"Unknown command: {cmd}"}
    except Exception as e:
        result = {"error": str(e)}

    print(json.dumps(result))


if __name__ == "__main__":
    main()
