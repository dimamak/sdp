"""Headless `codex exec` runner (Codex CLI / ChatGPT desktop backend).

Auth: `codex login` (ChatGPT subscription) or CODEX_API_KEY/OPENAI_API_KEY in
the environment. Subscription auth has a rolling 5-hour rate window that can
hard-block mid-run — a failure here always raises, so the caller's existing
notify() path surfaces it instead of silently degrading the post.

Codex cannot pre-assign a session id the way `claude -p --session-id` can:
`codex exec` has no --session-id flag (openai/codex#7801 still open as of this
writing). The id is only knowable *after* a call, from the `thread.started`
event in the --json stream, and a later turn resumes with
`codex exec resume <id>`. See llm.py for how this inverts the calling contract
in the abstraction on top of this module.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from ..util import get_logger

log = get_logger("pipeline.codex")


def run_codex(cfg, prompt: str, *, allow_read_dirs: list[str] | None = None,
             images: list[str] | None = None, timeout: int = 600,
             session_id: str | None = None, model: str | None = None) -> tuple[str, str | None]:
    """Run codex exec and return (result_text, thread_id).

    The prompt goes on stdin, via the trailing `-` — with a plain argument,
    codex treats stdin as extra context rather than the prompt itself. The
    final answer is read from the --output-last-message file, which is far
    more robust than picking the last item.completed/agent_message out of the
    --json event stream; that stream is only used here to capture the thread id.
    """
    codex_bin = str(cfg.get("pipeline.codex_bin", "codex"))
    with tempfile.TemporaryDirectory() as td:
        out_path = Path(td) / "last-message.txt"

        if session_id:
            cmd = [codex_bin, "exec", "resume", session_id, "--json", "-o", str(out_path)]
        else:
            cmd = [codex_bin, "exec", "--json", "--sandbox", "read-only",
                  "--skip-git-repo-check", "-o", str(out_path)]
            chosen_model = model or cfg.get("pipeline.model")
            if chosen_model:
                cmd += ["-m", str(chosen_model)]
        for img in (images or []):
            cmd += ["-i", str(img)]
        cmd.append("-")

        # No --allowedTools "" equivalent exists: --sandbox read-only is the
        # closest, but it still lets the model read files under the working
        # directory. There's also no per-call --add-dir-style grant for an
        # arbitrary extra directory (Codex's --add-dir grants *write* access,
        # not read) — running with cwd set to the first requested directory is
        # the closest honest approximation. Both gaps are real privacy/capability
        # differences from the Claude backend, documented in the README rather
        # than papered over.
        cwd = str(allow_read_dirs[0]) if allow_read_dirs else None
        if allow_read_dirs and len(allow_read_dirs) > 1:
            log.warning("codex backend: only the first of %d allow_read_dirs is reachable "
                       "(cwd=%s) — see README privacy section", len(allow_read_dirs), cwd)

        env_extra = {}
        key = cfg.secret("CODEX_API_KEY") or cfg.secret("OPENAI_API_KEY")
        if key:
            env_extra["CODEX_API_KEY"] = key

        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True, timeout=timeout, cwd=cwd,
            env={**os.environ, **env_extra},
        )
        if proc.returncode != 0:
            log.error("codex exec failed rc=%s argv=%s", proc.returncode, cmd)
            raise RuntimeError(
                f"codex exec failed (rc={proc.returncode}) "
                f"stderr={proc.stderr[:400]!r} stdout={proc.stdout[:400]!r}")

        thread_id = session_id
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = event.get("msg", event) if isinstance(event, dict) else None
            if isinstance(msg, dict) and msg.get("type") == "thread.started":
                thread_id = msg.get("thread_id") or thread_id

        try:
            text = out_path.read_text(encoding="utf-8").strip()
        except OSError as e:
            raise RuntimeError(f"codex exec produced no output file: {e}") from e

    if not text:
        raise RuntimeError("codex exec returned empty output")
    return text, thread_id
