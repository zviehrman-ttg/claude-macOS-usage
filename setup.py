"""py2app build configuration."""

from setuptools import setup

APP = ["claude_usage.py"]
DATA_FILES = []
OPTIONS = {
    "argv_emulation": False,
    "iconfile": "resources/icon.icns",
    "plist": {
        "CFBundleName": "Claude Usage Monitor",
        "CFBundleDisplayName": "Claude Usage Monitor",
        "CFBundleIdentifier": "com.claude.usage-monitor",
        "CFBundleVersion": "1.0.0",
        "CFBundleShortVersionString": "1.0.0",
        "LSUIElement": True,  # Hide from Dock (menu bar only)
        "NSHighResolutionCapable": True,
    },
    "packages": ["rumps", "requests", "certifi", "charset_normalizer", "urllib3", "idna"],
    "includes": ["src", "src.app", "src.auth", "src.config", "src.usage"],
}

setup(
    name="Claude Usage Monitor",
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
