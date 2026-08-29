"""Local always-on capture (audio, screen activity).

These modules run on the machine the person actually works on. In laptop mode
that is the same process as the bot (see server/bot/main.py); in server mode a
Windows laptop runs them as logon tasks and pushes the output over SSH.

Everything here is off unless explicitly enabled, and everything here honours a
`PAUSED` flag file in its output directory.
"""
from __future__ import annotations

import shutil
import sys

PAUSE_FILE = "PAUSED"

FFMPEG_INSTALL_HINT = {
    "win32": "winget install --id Gyan.FFmpeg -e",
    "darwin": "brew install ffmpeg",
}
FFMPEG_INSTALL_LINUX = "sudo apt install ffmpeg   (or: sudo dnf install ffmpeg)"


def ffmpeg_install_hint(platform: str | None = None) -> str:
    platform = platform or sys.platform
    return FFMPEG_INSTALL_HINT.get(platform, FFMPEG_INSTALL_LINUX)


def ensure_ffmpeg(platform: str | None = None) -> tuple[bool, str]:
    """(present, message). Never installs anything by itself — the wizard decides
    whether to run the hint, and on Linux it only ever prints it (no sudo from a
    setup script)."""
    missing = [b for b in ("ffmpeg", "ffprobe") if not shutil.which(b)]
    if not missing:
        return True, f"ffmpeg at {shutil.which('ffmpeg')}"
    return False, (f"{'/'.join(missing)} not on PATH — install with: "
                   f"{ffmpeg_install_hint(platform)}")


def capture_status_lines(cfg) -> list[str]:
    """One line per recorder, for /status and the doctor."""
    from .activity import status as activity_status
    from .audio import status as audio_status
    return [audio_status(cfg), activity_status(cfg)]
