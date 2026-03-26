"""Configuration constants for Claude Usage Monitor."""

import os

APP_NAME = "Claude Usage Monitor"
KEYCHAIN_SERVICE = "claude-usage-monitor"
KEYCHAIN_ACCOUNT_SESSION = "claude-session-key"
KEYCHAIN_ACCOUNT_ORG = "claude-preferred-org"

# Claude CLI credential in Keychain
CLI_KEYCHAIN_SERVICE = "Claude Code-credentials"

# claude.ai internal API (same backend as api.anthropic.com/api/)
CLAUDE_AI_URL = "https://claude.ai"
CLAUDE_AI_API = "https://claude.ai/api"
CLAUDE_SETTINGS_URL = "https://claude.ai/settings/usage"

# Paths
CLI_STATS_PATH = os.path.expanduser("~/.claude/stats-cache.json")

# Tier definitions — message limits are approximate (Anthropic doesn't publish exact numbers)
TIERS = {
    "free": {
        "name": "Free",
        "price": "$0/mo",
    },
    "pro": {
        "name": "Pro",
        "price": "$20/mo",
    },
    "max_5x": {
        "name": "Max 5x",
        "price": "$100/mo",
    },
    "max_20x": {
        "name": "Max 20x",
        "price": "$200/mo",
    },
    "enterprise": {
        "name": "Enterprise",
        "price": "Enterprise",
    },
}

# Map API rate_limit_tier values to our tier keys
TIER_MAP = {
    "free": "free",
    "pro": "pro",
    "default_claude_pro": "pro",
    "default_claude_max_5x": "max_5x",
    "max_5x": "max_5x",
    "max-5x": "max_5x",
    "default_claude_max_20x": "max_20x",
    "max_20x": "max_20x",
    "max-20x": "max_20x",
    "enterprise": "enterprise",
    "claude_enterprise": "enterprise",
    "default_claude_enterprise": "enterprise",
    "default_claude_ai": "enterprise",
    "default_raven_enterprise": "enterprise",
    "raven_enterprise": "enterprise",
    "business": "enterprise",
}

AUTO_REFRESH_INTERVAL = 300  # 5 minutes
