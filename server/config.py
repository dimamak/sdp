"""Config loader: config.yaml (settings) + .env (secrets).

Resolution order for the config file:
  1. --config CLI arg / explicit path passed to Config.load()
  2. $DAILYPOST_CONFIG
  3. <repo root>/config.yaml
The .env file is looked up next to the config file.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


def _parse_env_file(path: Path) -> dict:
    env = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


class Config:
    def __init__(self, data: dict, env: dict, path: Path):
        self.data = data
        self.env = env
        self.path = path

    @classmethod
    def load(cls, config_path: str | os.PathLike | None = None) -> "Config":
        p = Path(config_path or os.environ.get("DAILYPOST_CONFIG") or REPO_ROOT / "config.yaml")
        data = {}
        if p.exists():
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        env = _parse_env_file(p.parent / ".env")
        env.update(os.environ)  # real environment wins
        return cls(data, env, p)

    def get(self, dotted: str, default=None):
        cur = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def path_of(self, dotted: str, default=None) -> Path | None:
        v = self.get(dotted, default)
        return Path(os.path.expanduser(str(v))) if v else None

    def secret(self, key: str, default=None):
        v = self.env.get(key, default)
        return v if v not in ("", None) else default

    def sources(self, only_enabled: bool = True) -> list[dict]:
        out = []
        for s in self.get("sources", []) or []:
            if only_enabled and not s.get("enabled", False):
                continue
            out.append(s)
        return out

    def source_by_type(self, type_: str) -> dict | None:
        for s in self.sources(only_enabled=False):
            if s.get("type") == type_:
                return s
        return None
