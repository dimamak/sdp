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
               timeout: int = 600, session_id: str | None = None,
               resume: bool = False, allowed_tools: str | None = None) -> str:
    """Run claude -p and return the result text.

    session_id + resume=False starts a named session (so it can be resumed later);
    resume=True continues that session with the full prior conversation in context.
    """
    cmd = [
        str(cfg.get("pipeline.claude_bin", "claude")),
        "-p", "--output-format", "json",
        "--model", str(cfg.get("pipeline.model", "claude-haiku-4-5")),
    ]
    if session_id:
        cmd += (["--resume", session_id] if resume else ["--session-id", session_id])
    if allow_read_dirs:
        for d in allow_read_dirs:
            cmd += ["--add-dir", str(d)]
        cmd += ["--allowedTools", allowed_tools or "Read"]
    else:
        cmd += ["--allowedTools", allowed_tools if allowed_tools is not None else ""]

    # Credential precedence, most isolated first.
    #
    # The interactive login writes ~/.claude/.credentials.json, and Claude Code
    # ROTATES that OAuth token on refresh. When another process shares the same
    # home — an IDE/coding-agent session under the same user is the usual case —
    # whichever refreshes first revokes the other's token. The symptom is a call
    # that works once and then 401s "OAuth access token has been revoked".
    #
    # A token from `claude setup-token` is long-lived and passed by env, so it
    # never takes part in that rotation. Prefer it; ANTHROPIC_API_KEY is the
    # fully independent (API-priced) fallback.
    env_extra = {}
    oauth = cfg.secret("CLAUDE_CODE_OAUTH_TOKEN")
    key = cfg.secret("ANTHROPIC_API_KEY")
    if oauth:
        env_extra["CLAUDE_CODE_OAUTH_TOKEN"] = oauth
    elif key:
        env_extra["ANTHROPIC_API_KEY"] = key

    import os
    proc = subprocess.run(
        cmd, input=prompt, capture_output=True, text=True, timeout=timeout,
        env={**os.environ, **env_extra},
    )
    if proc.returncode != 0:
        # the CLI reports some failures on stdout with an empty stderr, so an
        # error built from stderr alone reads as a blank mystery
        raise RuntimeError(
            f"claude -p failed (rc={proc.returncode}) "
            f"stderr={proc.stderr[:600]!r} stdout={proc.stdout[:600]!r}")
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
