# Claude Usage Monitor

A macOS menu bar app that tracks your Claude AI usage limits in real time. See session and weekly utilization at a glance, monitor Claude Code CLI activity, and never get surprised by a rate limit again.

![macOS](https://img.shields.io/badge/macOS-only-blue) ![Python](https://img.shields.io/badge/Python-3.13%2B-yellow) ![License](https://img.shields.io/badge/License-MIT-green)

<!-- ![Screenshot](docs/screenshot.png) -->

<img width="403" height="688" alt="Screenshot 2026-03-06 at 18 01 56" src="https://github.com/user-attachments/assets/ff6e654c-9424-4877-8040-a30cdafbbae3" />


---

## Features

- **Real-time usage monitoring** -- View current session (5-hour window), weekly (all models), and weekly (Sonnet-only) utilization as progress bars directly from your menu bar.
- **Codex usage monitoring** -- Track Codex live rate-limit windows and local token usage alongside Claude.
- **Claude Code CLI stats** -- Messages sent, sessions started, tool calls made, and token usage broken down by model, for today and this week.
- **Auto-detect plan tier** -- Reads your Claude Code CLI credentials from macOS Keychain to determine whether you are on Free, Pro, Max 5x, or Max 20x.
- **Chrome cookie auto-extraction** -- Automatically extracts your `sessionKey` from Chrome cookies (with Keychain permission) so you can connect without any manual steps.
- **Manual session cookie connection** -- If auto-extraction is unavailable, paste your `sessionKey` from browser DevTools.
- **Extra usage billing display** -- Shows how much of your extra usage budget has been consumed (for plans that support it).
- **Reset countdown timers** -- See exactly when your session and weekly limits reset.
- **Compact provider submenus** -- Top-level menu stays short with at-a-glance summaries (usage + reset countdown), with full details under Claude/Codex submenus.
- **Color-coded status icon** -- The menu bar icon changes color based on your current session usage: green (< 50%), yellow (50-80%), orange (> 80%).
- **Secure credential storage** -- All session keys are stored in the macOS Keychain. Nothing is sent to any server other than `claude.ai`.
- **Auto-refresh** -- Usage data updates every 5 minutes in the background.

## Requirements

- **macOS** (uses macOS Keychain, `rumps` menu bar framework, and Chrome cookie decryption via `security` CLI)
- **Python 3.13+**
- **Claude Code CLI** installed and authenticated (for automatic tier detection and CLI stats)
- **Google Chrome** (optional, for automatic session cookie extraction)

## Installation

### Build from source

```bash
# Clone the repository
git clone https://github.com/your-username/claude-macOS-usage.git
cd claude-macOS-usage

# Run the build script
./scripts/build.sh
```

The build script will:
1. Create a Python virtual environment
2. Install all dependencies
3. Generate the app icon
4. Build the `.app` bundle with py2app

Once complete, install the app:

```bash
cp -r 'dist/Claude Usage Monitor.app' /Applications/
```

Or run it directly:

```bash
open 'dist/Claude Usage Monitor.app'
```

### Run without building

If you prefer to run the script directly without creating an app bundle:

```bash
# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run the app
python claude_usage.py
```

### Run with `mise` task

If you use `mise`, run the Swift menu bar host with:

```bash
mise r usage
```

## Usage

Once launched, the app lives in your macOS menu bar. Click the icon to open the dropdown menu.

### Menu overview

| Section | Description |
|---|---|
| **Header** | Your Claude account + plan and Codex account + plan. |
| **Claude / Codex rows** | Compact summaries with status color, current usage %, and `reset in ...` countdown. |
| **Provider submenus** | Full breakdown for each provider: progress bars, pace hints, reset times, and token-by-model details. |
| **More submenu** | Refresh, settings, org switching, and session connect/disconnect actions. |

### Connecting your session

The app tries to connect automatically on launch:

1. **Automatic (Chrome)** -- If you are logged into claude.ai in Chrome, the app attempts to extract your `sessionKey` cookie. macOS may prompt you to allow Keychain access.
2. **Manual** -- Click "Connect claude.ai Session..." and follow the instructions to copy the `sessionKey` cookie from your browser's DevTools.

Without a connected session the app still shows Claude Code CLI stats and estimated reset countdowns, but cannot display live utilization percentages.

### Enabling Codex data

Codex does not need manual setup inside this app. It is detected automatically from your local Codex files:

- `~/.codex/auth.json` (for live Codex usage API calls)
- `~/.codex/state_5.sqlite` (for local token history)

To enable Codex in the menu:

1. Sign in to Codex CLI with the account you want to track.
2. Use Codex at least once so local usage history exists.
3. Restart the menu app (`mise r usage`).

Quick check:

```bash
ls -l ~/.codex/auth.json ~/.codex/state_5.sqlite
```

### Changing your plan tier

If automatic detection picks the wrong tier, click **Tier** in the menu and select the correct plan. This affects the estimated usage calculations when live data is unavailable.

## How It Works

```
                          macOS Menu Bar
                               |
                 Swift Host (ClaudeUsageMonitor)
                               |
                           backend.py
                     /          |           \
            CLI Keychain     CLI Stats    APIs
            (tier, user)   (local usage)  (claude.ai + Codex)
```

1. **On launch**, the app reads Claude Code CLI credentials from the macOS Keychain (`Claude Code-credentials`) to detect your plan tier and username.
2. **CLI stats** are read from `~/.claude/stats-cache.json` and local project logs for token/message trends.
3. **Live Claude usage** is fetched from `claude.ai/api/organizations/{org_id}/usage` using your session cookie.
4. **Live Codex usage** is fetched from `chatgpt.com/backend-api/wham/usage` via local Codex auth tokens.
5. **Every 5 minutes**, the app refreshes local stats and live usage; compact menu summaries update with usage and reset countdowns.

## Security

- **Session keys** are stored exclusively in the macOS Keychain via the `keyring` library. They are never written to disk as plain text.
- **No external data transmission.** The app only communicates with `claude.ai` to fetch your usage data. No analytics, telemetry, or third-party services are involved.
- **Chrome cookie access** requires explicit Keychain permission. The app copies the Chrome cookie database to a temporary file, reads the relevant cookie, and immediately deletes the copy.
- **CLI credentials are read-only.** The app reads your existing Claude Code credentials from Keychain but never modifies them.

## Configuration

Configuration constants are defined in `src/config.py`:

| Constant | Default | Description |
|---|---|---|
| `AUTO_REFRESH_INTERVAL` | `300` (5 minutes) | How often usage data is refreshed, in seconds. |
| `CLI_STATS_PATH` | `~/.claude/stats-cache.json` | Path to Claude Code CLI stats file. |
| `CLAUDE_AI_API` | `https://claude.ai/api` | Base URL for the claude.ai internal API. |

## Project Structure

```
claude-macOS-usage/
  claude_usage.py          # Entry point
  setup.py                 # py2app build configuration
  requirements.txt         # Python dependencies
  src/
    __init__.py
    __main__.py
    app.py                 # Menu bar app class (ClaudeUsageApp)
    auth.py                # Authentication, cookie extraction, session validation
    config.py              # Constants and tier definitions
    usage.py               # Usage data fetching, parsing, and formatting
  scripts/
    build.sh               # Build script
    generate_icon.py       # Icon generator
  resources/
    icon.icns              # App icon
```

## Troubleshooting

**The app does not appear in the menu bar.**
Make sure you are running macOS and that `rumps` is installed correctly. If running from source, ensure the virtual environment is activated.

**"Connect claude.ai Session..." does not auto-extract the cookie.**
Chrome must be installed at the default path and you must be logged into claude.ai. macOS will prompt for Keychain access to "Chrome Safe Storage" -- you must click Allow. If Chrome's cookie database is locked, try closing Chrome first.

**Usage data shows 0% even though I have been using Claude.**
Verify your session is connected (the menu should show "Disconnect Session" rather than "Connect claude.ai Session..."). If the session has expired, disconnect and reconnect.

**Tier detection shows the wrong plan.**
Use the Tier submenu to manually override. Tier detection depends on Claude Code CLI credentials being present in the Keychain.

**CLI stats are not showing.**
Claude Code CLI must be installed and must have been used at least once so that `~/.claude/stats-cache.json` exists.

**The build fails with py2app errors.**
Ensure you are using Python 3.13+ and that all dependencies in `requirements.txt` are installed. Run the build inside a clean virtual environment.

## License

This project is licensed under the MIT License.

## Contributing

Contributions are welcome. Please open an issue to discuss proposed changes before submitting a pull request.

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Commit your changes (`git commit -m "Add my feature"`)
4. Push to the branch (`git push origin feature/my-feature`)
5. Open a pull request
