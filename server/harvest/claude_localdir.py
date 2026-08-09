"""Claude Code sessions from a plain (single-user) projects directory."""
from __future__ import annotations

from pathlib import Path

from . import register
from .claude_common import harvest_dir


@register("claude_projects_dir")
def collect(src, cfg, store, since) -> int:
    projects_dir = Path(str(src["projects_dir"])).expanduser()
    name = src.get("name", "claude")
    return harvest_dir(projects_dir, since, name, cfg, store)
