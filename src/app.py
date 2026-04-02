"""Main macOS menu bar application.

Data sources:
  1. Claude CLI credentials (Keychain) — plan tier detection
  2. Claude CLI stats-cache.json — local usage history
  3. claude.ai session cookie — live rate limit percentages
  4. Codex ~/.codex/state_5.sqlite — Codex token usage
"""

import threading
from datetime import datetime

import rumps

from .auth import (
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
    open_claude_login,
    open_claude_settings,
    save_preferred_org,
    save_session_key,
)
from .config import APP_NAME, AUTO_REFRESH_INTERVAL, TIER_MAP, TIERS
from .usage import (
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
from .codex import get_codex_stats, get_codex_live_usage, format_reset_time as codex_format_reset


def _run_on_main_thread(func):
    """Schedule a no-arg function to run on the main thread.

    Uses PyObjC's performSelectorOnMainThread to safely dispatch UI work
    back to the main run loop, avoiding AppKit threading violations that
    cause hangs and 'Not Responding' states.
    """
    from PyObjCTools import AppHelper
    AppHelper.callAfter(func)


def _noop(_):
    """No-op callback to keep menu items enabled (readable text)."""
    pass


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


class ClaudeUsageApp(rumps.App):
    def __init__(self):
        super().__init__(APP_NAME, title="\u2728", quit_button=None)
        self.tier = "pro"
        self.username = None
        self.org_id = None
        self.live_usage = None      # From claude.ai API
        self.claude_oauth_usage = None  # From api.anthropic.com/api/oauth/usage
        self.cli_stats = None       # From stats-cache.json
        self.claude_code_stats = None  # From ~/.claude/projects/*.jsonl
        self.codex_stats = None     # From ~/.codex/state_5.sqlite
        self.codex_live = None      # From chatgpt.com/backend-api/wham/usage
        self.last_refresh = None
        self.is_refreshing = False
        self.has_session = False
        self.has_cli_creds = False  # Cached to avoid subprocess in menu builds
        self.available_orgs = []

        self._detect_on_launch()
        self._build_menu()

        self.timer = rumps.Timer(self._auto_refresh, AUTO_REFRESH_INTERVAL)
        self.timer.start()

    # --- Startup ---

    def _select_org(self, chat_orgs):
        """Pick the best org from available chat orgs using saved preference."""
        preferred = get_preferred_org()
        if preferred:
            for org in chat_orgs:
                if org["org_id"] == preferred:
                    return org
        return chat_orgs[0] if chat_orgs else None

    def _detect_on_launch(self):
        """Detect CLI credentials and existing session on launch (off main thread)."""
        def _detect():
            tier = detect_tier_from_cli()
            username = get_cli_username()
            cli_stats = get_cli_stats()
            claude_code_stats = get_claude_code_token_stats()
            codex_stats = get_codex_stats()
            codex_live = get_codex_live_usage()
            claude_oauth = fetch_claude_oauth_usage()
            cli_creds = has_cli_credentials()

            org_id = None
            has_session = False
            session_tier = None
            chat_orgs = []

            # Check for existing session key
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

            # Select org if we have any
            if chat_orgs:
                has_session = True
                selected = self._select_org(chat_orgs)
                if selected:
                    org_id = selected["org_id"]
                    session_tier = selected.get("rate_limit_tier")

            # Apply results on main thread
            def _apply():
                # Prefer the org's live rate_limit_tier over the CLI-cached one
                resolved_tier = TIER_MAP.get(session_tier) if session_tier else None
                if resolved_tier:
                    self.tier = resolved_tier
                elif tier:
                    self.tier = tier
                self.username = username
                self.cli_stats = cli_stats
                self.claude_code_stats = claude_code_stats
                self.codex_stats = codex_stats
                self.codex_live = codex_live
                self.claude_oauth_usage = claude_oauth
                self.has_cli_creds = cli_creds
                self.org_id = org_id
                self.has_session = has_session
                self.available_orgs = chat_orgs
                self._build_menu()

                if self.has_session:
                    self._refresh_data()
                elif self.cli_stats:
                    self._update_title_icon()

            _run_on_main_thread(_apply)

        threading.Thread(target=_detect, daemon=True).start()

    # --- Menu ---

    def _build_menu(self):
        self.menu.clear()

        tier_info = TIERS.get(self.tier, TIERS["pro"])
        resets = get_reset_countdown()

        # Header — omit price if it's the same as the plan name (e.g. Enterprise)
        price = tier_info["price"]
        tier_label = tier_info["name"] if price == tier_info["name"] else f"{tier_info['name']} ({price})"
        if self.username:
            header = f"{self.username} - {tier_label}"
        else:
            header = tier_label
        codex_identity = _format_codex_identity(self.codex_live)
        if codex_identity:
            header = f"{header} / {codex_identity}"

        self.menu.add(rumps.MenuItem(header, callback=_noop))
        self.menu.add(rumps.separator)

        # ---- Claude Usage header ----
        self.menu.add(rumps.MenuItem("── Claude ──", callback=_noop))

        # ---- Live usage (from claude.ai session) ----
        if self.live_usage:
            plan_mode = self.live_usage.get("plan_mode", "token_cap")

            if plan_mode == "spend_cap":
                spend = self.live_usage["spend"]
                pct = spend["percent"]
                amount = spend["amount"]
                limit = spend["limit"]
                reset = spend["reset_at"]
                self.menu.add(rumps.MenuItem("  Monthly spend", callback=_noop))
                self.menu.add(rumps.MenuItem(
                    f"    {build_bar(pct)}  {pct}% used",
                    callback=_noop,
                ))
                self.menu.add(rumps.MenuItem(
                    f"    ${amount:.2f} of ${limit:.2f} spent",
                    callback=_noop,
                ))
                if reset:
                    self.menu.add(rumps.MenuItem(f"    Resets {reset}", callback=_noop))
                self.menu.add(rumps.separator)

            elif plan_mode == "unknown":
                self.menu.add(rumps.MenuItem("  Usage data unavailable for this plan", callback=_noop))
                self.menu.add(rumps.MenuItem("  See claude.ai/settings/usage", callback=_noop))
                self.menu.add(rumps.separator)

            else:
                # token_cap: Pro / Max plans
                for key in ("session", "weekly_all", "weekly_sonnet"):
                    bucket = self.live_usage[key]
                    pct = bucket["percent"]
                    reset = bucket["reset_at"]
                    label = bucket["label"]
                    resets_at_iso = bucket.get("resets_at_iso", "")
                    window_h = 5 if key == "session" else 168
                    pace = predict_pace(pct, resets_at_iso, window_h)

                    self.menu.add(rumps.MenuItem(f"  {label}", callback=_noop))
                    self.menu.add(rumps.MenuItem(
                        f"    {build_bar(pct)}  {pct}% used",
                        callback=_noop,
                    ))
                    if pace:
                        self.menu.add(rumps.MenuItem(f"{pace}  [{key}]", callback=_noop))
                    if reset:
                        self.menu.add(rumps.MenuItem(f"    Resets {reset}", callback=_noop))
                    self.menu.add(rumps.separator)

        elif self.has_session:
            self.menu.add(rumps.MenuItem("  Loading live usage...", callback=_noop))
            self.menu.add(rumps.separator)

        elif self.claude_oauth_usage:
            # OAuth usage from CLI token (no session cookie needed)
            ou = self.claude_oauth_usage
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
                self.menu.add(rumps.MenuItem(label, callback=_noop))
                self.menu.add(rumps.MenuItem(
                    f"    {build_bar(int(pct))}  {int(pct)}% used",
                    callback=_noop,
                ))
                if pace:
                    self.menu.add(rumps.MenuItem(f"{pace}  [{key}]", callback=_noop))
                if resets_at:
                    self.menu.add(rumps.MenuItem(f"    Resets {usage_format_reset(resets_at)}", callback=_noop))
                self.menu.add(rumps.separator)

            # Extra (add-on) credits
            extra = ou.get("extra_usage") or {}
            if extra.get("is_enabled"):
                used = int(extra.get("used_credits", 0))
                limit = int(extra.get("monthly_limit", 0))
                pct = min(int(extra.get("utilization", 0)), 100)
                warn = "  ⚠️ Add-on credits" if pct >= 100 else "  Add-on credits"
                self.menu.add(rumps.MenuItem(warn, callback=_noop))
                self.menu.add(rumps.MenuItem(
                    f"    {build_bar(pct)}  {used}/{limit} used ({pct}%)",
                    callback=_noop,
                ))
                self.menu.add(rumps.separator)
        else:
            # No session — show resets based on time
            self.menu.add(rumps.MenuItem("  Usage limits (connect for live data)", callback=_noop))
            self.menu.add(rumps.separator)
            self.menu.add(rumps.MenuItem(f"    Daily resets in:  {resets['daily']}", callback=_noop))
            self.menu.add(rumps.MenuItem(f"    Weekly resets in: {resets['weekly']}", callback=_noop))
            self.menu.add(rumps.separator)

        # ---- Claude Code Token Stats (from ~/.claude/projects jsonl files) ----
        token_stats = self.claude_code_stats or self.cli_stats
        if token_stats:
            if token_stats.get("today_tokens_by_model"):
                self.menu.add(rumps.separator)
                self.menu.add(rumps.MenuItem("    Tokens today:", callback=_noop))
                for model, count in sorted(
                    token_stats["today_tokens_by_model"].items(),
                    key=lambda x: -x[1],
                ):
                    name = shorten_model_name(model)
                    self.menu.add(rumps.MenuItem(
                        f"      {name}: {format_tokens(count)}",
                        callback=_noop,
                    ))

            if token_stats.get("week_tokens_by_model"):
                self.menu.add(rumps.separator)
                self.menu.add(rumps.MenuItem("    Tokens this week:", callback=_noop))
                for model, count in sorted(
                    token_stats["week_tokens_by_model"].items(),
                    key=lambda x: -x[1],
                ):
                    name = shorten_model_name(model)
                    self.menu.add(rumps.MenuItem(
                        f"      {name}: {format_tokens(count)}",
                        callback=_noop,
                    ))

        # ---- Codex Usage ----
        if self.codex_stats or self.codex_live:
            self.menu.add(rumps.separator)
            live = self.codex_live

            # Header: plan name
            if live:
                codex_plan = live.get("plan_label") or live.get("plan_name", "Unknown")
                header = f"── Codex ({codex_plan}) ──"
            else:
                header = "── Codex ──"
            self.menu.add(rumps.MenuItem(header, callback=_noop))

            # Live rate-limit windows
            if live:
                p_pct = live["primary_pct"]
                p_h = live["primary_window_h"]
                p_reset = codex_format_reset(live["primary_reset_s"])
                p_pace = predict_pace(p_pct, live.get("primary_resets_at"), p_h)
                self.menu.add(rumps.MenuItem(
                    f"  {build_bar(p_pct)}  {p_pct}% used ({p_h}h window)",
                    callback=_noop,
                ))
                if p_pace:
                    self.menu.add(rumps.MenuItem(p_pace, callback=_noop))
                self.menu.add(rumps.MenuItem(f"    Resets in {p_reset}", callback=_noop))

                s_pct = live["secondary_pct"]
                if s_pct > 0 or live["secondary_window_h"] > 0:
                    s_h = live["secondary_window_h"]
                    s_reset = codex_format_reset(live["secondary_reset_s"])
                    s_resets_at = live.get("secondary_resets_at")
                    s_pace = predict_pace(s_pct, s_resets_at, s_h)
                    self.menu.add(rumps.MenuItem(
                        f"  {build_bar(s_pct)}  {s_pct}% used ({s_h}h window)",
                        callback=_noop,
                    ))
                    if s_pace:
                        self.menu.add(rumps.MenuItem(s_pace, callback=_noop))
                    self.menu.add(rumps.MenuItem(f"    Resets in {s_reset}", callback=_noop))
            else:
                self.menu.add(rumps.MenuItem("  (not connected)", callback=_noop))

            # Local token history
            if self.codex_stats:
                stats = self.codex_stats
                if stats["today_tokens_by_model"]:
                    self.menu.add(rumps.separator)
                    self.menu.add(rumps.MenuItem("    Tokens today:", callback=_noop))
                    for model, count in stats["today_tokens_by_model"].items():
                        self.menu.add(rumps.MenuItem(
                            f"      {model}: {format_tokens(count)}",
                            callback=_noop,
                        ))
                if stats["week_tokens_by_model"]:
                    self.menu.add(rumps.separator)
                    self.menu.add(rumps.MenuItem("    Tokens this week:", callback=_noop))
                    for model, count in stats["week_tokens_by_model"].items():
                        self.menu.add(rumps.MenuItem(
                            f"      {model}: {format_tokens(count)}",
                            callback=_noop,
                        ))

        self.menu.add(rumps.separator)

        # ---- Timestamp ----
        if self.last_refresh:
            ts = self.last_refresh.strftime("%H:%M:%S")
            self.menu.add(rumps.MenuItem(f"Last updated: {ts}", callback=_noop))
        self.menu.add(rumps.separator)

        # ---- Actions ----
        refresh_label = "Refreshing..." if self.is_refreshing else "Refresh Now"
        self.menu.add(rumps.MenuItem(refresh_label, callback=self._on_refresh))
        self.menu.add(rumps.MenuItem("Open claude.ai/settings/usage", callback=self._on_open_settings))

        # Organization switcher (only when multiple orgs available)
        if len(self.available_orgs) > 1:
            switch_menu = rumps.MenuItem("Switch Organization")
            for org in self.available_orgs:
                prefix = "\u25CF " if org["org_id"] == self.org_id else "   "
                label = f"{prefix}{org['name']}"
                item = rumps.MenuItem(
                    label,
                    callback=lambda sender, o=org: self._on_switch_org(o),
                )
                switch_menu.add(item)
            self.menu.add(switch_menu)

        # Session management
        if self.has_session:
            self.menu.add(rumps.MenuItem("Disconnect Session", callback=self._on_disconnect))
        else:
            self.menu.add(rumps.MenuItem(
                "Connect claude.ai Session...",
                callback=self._on_connect_session,
            ))

        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("Quit", callback=self._on_quit))

    # --- Data refresh ---

    def _refresh_data(self):
        if self.is_refreshing:
            return
        self.is_refreshing = True
        self._build_menu()

        def _fetch():
            try:
                # Refresh CLI stats and Codex stats
                cli_stats = get_cli_stats()
                claude_code_stats = get_claude_code_token_stats()
                codex_stats = get_codex_stats()
                codex_live = get_codex_live_usage()
                claude_oauth = fetch_claude_oauth_usage()

                # Fetch live usage if we have a session
                live = None
                expired = False
                if self.has_session:
                    session_key = get_session_key()
                    if session_key and self.org_id:
                        live = fetch_claude_ai_usage(session_key, self.org_id)
                        if live and live.get("expired"):
                            expired = True
                            live = None

                # Dispatch all state + UI updates back to the main thread
                def _apply():
                    self.cli_stats = cli_stats
                    self.claude_code_stats = claude_code_stats
                    self.codex_stats = codex_stats
                    self.codex_live = codex_live
                    self.claude_oauth_usage = claude_oauth
                    if expired:
                        delete_session_key()
                        self.has_session = False
                        self.org_id = None
                        self.live_usage = None
                    elif live:
                        self.live_usage = live
                    self.last_refresh = datetime.now()
                    self.is_refreshing = False
                    self._update_title_icon()
                    self._build_menu()

                _run_on_main_thread(_apply)
            except Exception:
                def _reset():
                    self.is_refreshing = False
                    self._build_menu()
                _run_on_main_thread(_reset)

        threading.Thread(target=_fetch, daemon=True).start()

    def _update_title_icon(self):
        # Claude dot
        if self.live_usage:
            plan_mode = self.live_usage.get("plan_mode", "token_cap")
            if plan_mode == "spend_cap" and self.live_usage.get("spend"):
                pct = self.live_usage["spend"]["percent"]
            elif plan_mode == "token_cap":
                pct = self.live_usage["session"]["percent"]
            else:
                claude_dot = "\u2728"
                pct = None
        elif self.cli_stats and self.cli_stats["today_messages"] > 0:
            tier_daily = {"free": 25, "pro": 100, "max_5x": 500, "max_20x": 2000}
            limit = tier_daily.get(self.tier, 100)
            pct = min(int(self.cli_stats["today_messages"] / limit * 100), 100)
        else:
            pct = None

        if pct is not None:
            if pct > 80:
                claude_dot = "\U0001F7E0"   # Orange
            elif pct > 50:
                claude_dot = "\U0001F7E1"   # Yellow
            else:
                claude_dot = "\U0001F7E2"   # Green
        else:
            claude_dot = "\u2728"

        # Codex dot: red if limit reached, orange/yellow/green by primary pct, blue if no data
        if self.codex_live:
            live_c = self.codex_live
            if live_c.get("limit_reached"):
                codex_dot = "\U0001F534"  # Red
            elif live_c["primary_pct"] > 80:
                codex_dot = "\U0001F7E0"  # Orange
            elif live_c["primary_pct"] > 50:
                codex_dot = "\U0001F7E1"  # Yellow
            else:
                codex_dot = "\U0001F7E2"  # Green
        elif self.codex_stats:
            codex_dot = "\U0001F535"  # Blue (connected but no live)
        else:
            codex_dot = ""

        # Short plan labels for menu bar
        tier_short = {
            "free": "Free", "pro": "Pro", "max_5x": "Max 5x", "max_20x": "Max 20x",
        }.get(self.tier, self.tier.title() if self.tier else "")

        codex_plan = self.codex_live["plan_name"] if self.codex_live else ""

        if codex_dot:
            self.title = f"{claude_dot} / {codex_dot}"
        else:
            self.title = claude_dot

    def _auto_refresh(self, _):
        self._refresh_data()

    # --- Callbacks ---

    def _on_refresh(self, _):
        self._refresh_data()

    def _on_open_settings(self, _):
        open_claude_settings()

    def _on_connect_session(self, _):
        def _try_auto_connect():
            chrome_key = extract_chrome_session_key()
            if chrome_key:
                chat_orgs = get_chat_organizations(chrome_key)
                if chat_orgs:
                    save_session_key(chrome_key)
                    selected = self._select_org(chat_orgs)
                    def _apply():
                        self.org_id = selected["org_id"]
                        tier = TIER_MAP.get(selected.get("rate_limit_tier", ""))
                        if tier:
                            self.tier = tier
                        self.has_session = True
                        self.available_orgs = chat_orgs
                        msg = "Session extracted from Chrome cookies."
                        if len(chat_orgs) > 1:
                            msg += " Multiple orgs found — use Switch Organization."
                        rumps.notification(APP_NAME, "Connected automatically!", msg)
                        self._build_menu()
                        self._refresh_data()
                    _run_on_main_thread(_apply)
                    return

            # Auto-extract failed — show manual dialog on main thread
            _run_on_main_thread(self._show_manual_connect_dialog)

        threading.Thread(target=_try_auto_connect, daemon=True).start()

    def _show_manual_connect_dialog(self):
        instructions = get_session_cookie_instructions()
        window = rumps.Window(
            message=instructions,
            title="Connect claude.ai Session",
            default_text="",
            ok="Connect",
            cancel="Cancel",
            dimensions=(380, 24),
        )
        response = window.run()
        if not response.clicked or not response.text.strip():
            return

        session_key = response.text.strip().strip("'\"")

        def _validate():
            chat_orgs = get_chat_organizations(session_key)

            def _apply():
                if chat_orgs:
                    save_session_key(session_key)
                    selected = self._select_org(chat_orgs)
                    self.org_id = selected["org_id"]
                    tier = TIER_MAP.get(selected.get("rate_limit_tier", ""))
                    if tier:
                        self.tier = tier
                    self.has_session = True
                    self.available_orgs = chat_orgs
                    rumps.notification(APP_NAME, "Connected!", f"Org: {selected.get('name', selected['org_id'][:12])}")
                    self._build_menu()
                    self._refresh_data()
                else:
                    rumps.notification(
                        APP_NAME,
                        "Connection failed",
                        "Invalid or expired session cookie. Try again.",
                    )
            _run_on_main_thread(_apply)

        threading.Thread(target=_validate, daemon=True).start()

    def _on_disconnect(self, _):
        delete_session_key()
        delete_preferred_org()
        self.has_session = False
        self.org_id = None
        self.live_usage = None
        self.available_orgs = []
        self.title = "\u2728"
        self._build_menu()

    def _on_switch_org(self, org):
        def _do_switch():
            save_preferred_org(org["org_id"])
            def _apply():
                self.org_id = org["org_id"]
                tier = TIER_MAP.get(org.get("rate_limit_tier", ""))
                if tier:
                    self.tier = tier
                self.live_usage = None
                self._build_menu()
                self._refresh_data()
            _run_on_main_thread(_apply)
        threading.Thread(target=_do_switch, daemon=True).start()

    def _on_quit(self, _):
        rumps.quit_application()


def _set_process_name():
    """Set the macOS process name so notifications show 'Claude Usage Monitor' instead of 'Python'."""
    try:
        from Foundation import NSBundle
        bundle = NSBundle.mainBundle()
        info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
        if info:
            info["CFBundleName"] = APP_NAME
    except ImportError:
        pass


def main():
    _set_process_name()
    ClaudeUsageApp().run()
