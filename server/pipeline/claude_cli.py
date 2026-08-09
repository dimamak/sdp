"""Headless `claude -p` runner (uses the host's existing Claude Code credentials).

Deployments that prefer the API can set ANTHROPIC_API_KEY in .env — the CLI
picks it up from the environment automatically.
"""
from __future__ import annotations

import json
import re
import subprocess

from ..util import get_logger

log = get_logger("pipeline.claude")


def run_claude(cfg, prompt: str, *, allow_read_dirs: list[str] | None = None,
               timeout: int = 600) -> str:
    """Run claude -p and return the result text."""
    cmd = [
        str(cfg.get("pipeline.claude_bin", "claude")),
        "-p", "--output-format", "json",
        "--model", str(cfg.get("pipeline.model", "claude-haiku-4-5")),
    ]
    if allow_read_dirs:
        for d in allow_read_dirs:
            cmd += ["--add-dir", str(d)]
        cmd += ["--allowedTools", "Read"]
    else:
        cmd += ["--allowedTools", ""]  # pure text task, no tools

    env_extra = {}
    key = cfg.secret("ANTHROPIC_API_KEY")
    if key:
        env_extra["ANTHROPIC_API_KEY"] = key

    import os
    proc = subprocess.run(
        cmd, input=prompt, capture_output=True, text=True, timeout=timeout,
        env={**os.environ, **env_extra},
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p failed (rc={proc.returncode}): {proc.stderr[:1000]}")
    data = json.loads(proc.stdout)
    if data.get("is_error"):
        raise RuntimeError(f"claude -p returned error: {str(data.get('result'))[:1000]}")
    return data.get("result", "")


def extract_json(text: str) -> dict:
    """Parse the first JSON object found in a model response (tolerates fences/prose)."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        return json.loads(fenced.group(1))
    start = text.find("{")
    if start == -1:
        raise ValueError(f"no JSON object in response: {text[:300]}")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("unbalanced JSON in response")
