"""Backend-agnostic LLM runner — dispatches to the Claude or Codex CLI.

`run_claude` (claude_cli.py) is Claude-specific: the caller mints a session id
up front and hands it to the CLI. Codex has no equivalent — `codex exec` has
no --session-id flag, so a session id can only be discovered *after* the first
call, from the `thread.started` event. `run_llm` inverts the contract so both
backends fit it: pass session=None to start a fresh conversation (and get an
id back either way), session=<id> to resume one.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from .claude_cli import run_claude
from .codex_cli import run_codex


@dataclass
class LLMResult:
    text: str
    session_id: str | None
    backend: str


def run_llm(cfg, prompt: str, *, session: str | None = None,
           allow_read_dirs: list[str] | None = None,
           images: list[str] | None = None,
           timeout: int = 600, allowed_tools: str | None = None,
           model: str | None = None) -> LLMResult:
    """Run one turn against the configured pipeline.backend.

    `images` is Codex-only (repeated -i flags); the Claude backend ignores it
    since its vision path is Read-tool-based (allow_read_dirs + allowed_tools).
    `allowed_tools` is Claude-only for the same reason, in reverse.
    """
    backend = str(cfg.get("pipeline.backend", "claude") or "claude")
    if backend == "codex":
        text, thread_id = run_codex(
            cfg, prompt, session_id=session, allow_read_dirs=allow_read_dirs,
            images=images, timeout=timeout, model=model)
        return LLMResult(text=text, session_id=thread_id, backend="codex")

    # Claude mints a uuid up front when starting fresh — same behaviour as
    # before this abstraction existed, just relocated from draft.py.
    session_id = session or str(uuid.uuid4())
    text = run_claude(
        cfg, prompt, session_id=session_id, resume=bool(session),
        allow_read_dirs=allow_read_dirs, timeout=timeout,
        allowed_tools=allowed_tools, model=model)
    return LLMResult(text=text, session_id=session_id, backend="claude")
