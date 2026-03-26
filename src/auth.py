"""Authentication: reads Claude CLI credentials + browser session cookie."""

import json
import logging
import os
import sqlite3
import subprocess
import shutil
import tempfile
import webbrowser

import keyring
import requests

from .config import (
    CLI_KEYCHAIN_SERVICE,
    CLI_STATS_PATH,
    CLAUDE_AI_API,
    CLAUDE_AI_URL,
    CLAUDE_SETTINGS_URL,
    KEYCHAIN_ACCOUNT_ORG,
    KEYCHAIN_ACCOUNT_SESSION,
    KEYCHAIN_SERVICE,
    TIER_MAP,
)


# --- CLI credentials (read-only, from Claude Code keychain entry) ---

def get_cli_credentials():
    """Read Claude CLI OAuth credentials from macOS Keychain.

    Returns dict with accessToken, rateLimitTier, subscriptionType, etc.
    or None if not found.
    """
    try:
        result = subprocess.run(
            [
                "security", "find-generic-password",
                "-s", CLI_KEYCHAIN_SERVICE,
                "-w",
            ],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout.strip())
        oauth = data.get("claudeAiOauth", {})
        return {
            "access_token": oauth.get("accessToken"),
            "refresh_token": oauth.get("refreshToken"),
            "subscription_type": oauth.get("subscriptionType", ""),
            "rate_limit_tier": oauth.get("rateLimitTier", ""),
            "scopes": oauth.get("scopes", []),
        }
    except (subprocess.SubprocessError, json.JSONDecodeError, KeyError):
        return None


def get_cli_username():
    """Get the username from the CLI keychain entry."""
    try:
        result = subprocess.run(
            [
                "security", "find-generic-password",
                "-s", CLI_KEYCHAIN_SERVICE,
            ],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if '"acct"' in line and '<blob>=' in line:
                return line.split('<blob>="')[1].rstrip('"')
    except (subprocess.SubprocessError, IndexError):
        pass
    return None


def detect_tier_from_cli():
    """Detect plan tier from CLI credentials."""
    creds = get_cli_credentials()
    if not creds:
        return None
    tier_raw = creds.get("rate_limit_tier", "")
    return TIER_MAP.get(tier_raw, "pro")


# --- Session cookie management ---

def get_session_key():
    """Get manually stored session key from our app's Keychain."""
    return keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT_SESSION)


def save_session_key(session_key):
    keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT_SESSION, session_key)


def delete_session_key():
    try:
        keyring.delete_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT_SESSION)
    except keyring.errors.PasswordDeleteError:
        pass


def get_preferred_org():
    """Get the preferred org ID from Keychain."""
    return keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT_ORG)


def save_preferred_org(org_id):
    """Save the preferred org ID to Keychain."""
    keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT_ORG, org_id)


def delete_preferred_org():
    """Delete the preferred org from Keychain."""
    try:
        keyring.delete_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT_ORG)
    except keyring.errors.PasswordDeleteError:
        pass


def extract_chrome_session_key():
    """Try to extract claude.ai sessionKey from Chrome cookies.

    This function reads Chrome's local cookie SQLite database (a copy is made
    to avoid lock conflicts) and, if the cookie is encrypted, decrypts it using
    Chrome's Safe Storage key from the macOS Keychain. The user will be prompted
    by macOS to grant Keychain access the first time this runs.

    Note: This couples to Chrome's internal cookie storage format (AES-CBC with
    PBKDF2-derived key, salt='saltysalt', 1003 iterations). Changes to Chrome's
    encryption scheme may break this functionality.

    Returns the sessionKey value or None.
    """
    chrome_cookie_path = os.path.expanduser(
        "~/Library/Application Support/Google/Chrome/Default/Cookies"
    )
    if not os.path.exists(chrome_cookie_path):
        return None

    try:
        # Copy the cookie DB to avoid lock conflicts with Chrome
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp_path = tmp.name
        shutil.copy2(chrome_cookie_path, tmp_path)

        conn = sqlite3.connect(tmp_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT encrypted_value, value FROM cookies "
            "WHERE host_key LIKE '%claude.ai%' AND name = 'sessionKey' "
            "ORDER BY last_access_utc DESC LIMIT 1"
        )
        row = cursor.fetchone()
        conn.close()
        os.unlink(tmp_path)

        if not row:
            return None

        encrypted_value, plain_value = row
        # If there's a plain text value, use it
        if plain_value:
            return plain_value

        # Try to decrypt using Chrome's Keychain key
        decrypted = _decrypt_chrome_cookie(encrypted_value)
        if decrypted and _is_valid_session_key(decrypted):
            return decrypted
        return None

    except (sqlite3.Error, OSError):
        return None


def _is_valid_session_key(value):
    """Check that a session key contains only ASCII-printable characters."""
    try:
        value.encode("latin-1")
        return len(value) > 10 and value.isprintable()
    except (UnicodeEncodeError, AttributeError):
        return False


def _decrypt_chrome_cookie(encrypted_value):
    """Decrypt a Chrome cookie value using the Safe Storage key from Keychain."""
    if not encrypted_value:
        return None

    # Chrome v10+ cookies start with b'v10' or b'v11'
    if encrypted_value[:3] not in (b"v10", b"v11"):
        return None

    try:
        # Get Chrome's encryption key from Keychain
        result = subprocess.run(
            [
                "security", "find-generic-password",
                "-s", "Chrome Safe Storage",
                "-w",
            ],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None

        chrome_password = result.stdout.strip()

        # Derive key using PBKDF2
        import hashlib
        key = hashlib.pbkdf2_hmac(
            "sha1",
            chrome_password.encode("utf-8"),
            b"saltysalt",
            1003,
            dklen=16,
        )

        # Decrypt AES-CBC
        try:
            from Crypto.Cipher import AES
        except ImportError:
            logging.debug("pycryptodome not installed — cannot decrypt Chrome cookies")
            return None
        iv = b" " * 16
        cipher = AES.new(key, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(encrypted_value[3:])
        # Remove PKCS7 padding
        padding_len = decrypted[-1]
        if isinstance(padding_len, int) and 1 <= padding_len <= 16:
            decrypted = decrypted[:-padding_len]
        return decrypted.decode("utf-8", errors="ignore")

    except Exception as e:
        logging.debug("Failed to decrypt Chrome cookie: %s", e)
        return None


# --- claude.ai API calls ---

def _session_headers(session_key):
    return {
        "cookie": f"sessionKey={session_key}",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "accept": "application/json",
    }


def validate_session(session_key):
    """Validate session and return the best org for usage tracking, or None.

    Picks the org with a stripe subscription (Pro/Max) over free/API orgs.
    """
    try:
        resp = requests.get(
            f"{CLAUDE_AI_API}/organizations",
            headers=_session_headers(session_key),
            timeout=10,
            verify=True,
        )
        if resp.status_code != 200:
            return None
        orgs = resp.json()
        if not isinstance(orgs, list) or len(orgs) == 0:
            return None

        # Prefer the org with a paid chat subscription.
        # Priority: contracted enterprise > standard stripe > any chat org
        best_org = orgs[0]
        best_priority = -1
        for org in orgs:
            billing = org.get("billing_type", "") or ""
            caps = org.get("capabilities", [])
            if "chat" not in caps:
                continue
            if billing == "stripe_subscription_contracted":
                priority = 2
            elif billing == "stripe_subscription":
                priority = 1
            elif billing:
                priority = 0
            else:
                priority = -1
            if priority > best_priority:
                best_priority = priority
                best_org = org

        return {
            "org_id": best_org.get("uuid", best_org.get("id", "")),
            "name": best_org.get("name", ""),
            "billing_type": best_org.get("billing_type", ""),
            "rate_limit_tier": best_org.get("rate_limit_tier", ""),
        }
    except requests.RequestException:
        pass
    return None


def get_chat_organizations(session_key):
    """Fetch all chat-capable organizations for the session.

    Returns list of dicts with org_id, name, billing_type, rate_limit_tier.
    Empty list if session is invalid.
    """
    try:
        resp = requests.get(
            f"{CLAUDE_AI_API}/organizations",
            headers=_session_headers(session_key),
            timeout=10,
            verify=True,
        )
        if resp.status_code != 200:
            return []
        orgs = resp.json()
        if not isinstance(orgs, list):
            return []

        chat_orgs = []
        for org in orgs:
            caps = org.get("capabilities", [])
            if "chat" not in caps:
                continue
            chat_orgs.append({
                "org_id": org.get("uuid", org.get("id", "")),
                "name": org.get("name", ""),
                "billing_type": org.get("billing_type", ""),
                "rate_limit_tier": org.get("rate_limit_tier", ""),
            })
        return chat_orgs
    except requests.RequestException:
        return []


# --- Helpers ---

def has_cli_credentials():
    return get_cli_credentials() is not None


def open_claude_settings():
    webbrowser.open(CLAUDE_SETTINGS_URL)


def open_claude_login():
    webbrowser.open(CLAUDE_AI_URL)


def get_session_cookie_instructions():
    return (
        "To connect your claude.ai account:\n\n"
        "1. Open claude.ai in your browser and log in\n"
        "2. Open DevTools (Cmd+Option+I)\n"
        "3. Go to Application > Cookies > claude.ai\n"
        "4. Find the 'sessionKey' cookie\n"
        "5. Copy its Value and paste it here\n\n"
        "Stored securely in your macOS Keychain."
    )
