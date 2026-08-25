import json
from datetime import datetime, timezone

from server.pipeline.digest import _compress_codex_jsonl, _compress_jsonl


def _write_jsonl(path, records):
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


# ---------------------------------------------------------------------------
# Claude Code session transcripts (_compress_jsonl)
# ---------------------------------------------------------------------------

def test_compress_jsonl_extracts_user_and_assistant_text(tmp_path):
    path = tmp_path / "session.jsonl"
    _write_jsonl(path, [
        {"cwd": "/home/me/project", "gitBranch": "main",
         "message": {"role": "user", "content": "fix the flaky test"}},
        {"message": {"role": "assistant", "content": [
            {"type": "text", "text": "Found it, race in the setup fixture."},
        ]}},
    ])
    out = _compress_jsonl(path)
    assert "(project: /home/me/project, branch: main)" in out
    assert "USER: fix the flaky test" in out
    assert "ASSISTANT: Found it, race in the setup fixture." in out


def test_compress_jsonl_drops_command_and_caveat_noise(tmp_path):
    path = tmp_path / "session.jsonl"
    _write_jsonl(path, [
        {"message": {"role": "user", "content": "<command-name>clear</command-name>"}},
        {"message": {"role": "user", "content": "Caveat: something internal"}},
        {"message": {"role": "user", "content": "real question here"}},
    ])
    out = _compress_jsonl(path)
    assert "real question here" in out
    assert "clear" not in out
    assert "Caveat" not in out


def test_compress_jsonl_respects_since_window(tmp_path):
    path = tmp_path / "session.jsonl"
    _write_jsonl(path, [
        {"timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": "old message, outside window"}},
        {"timestamp": "2026-01-02T12:00:00Z",
         "message": {"role": "user", "content": "new message, inside window"}},
    ])
    since = datetime(2026, 1, 2, tzinfo=timezone.utc)
    out = _compress_jsonl(path, since=since)
    assert "new message" in out
    assert "old message" not in out


def test_compress_jsonl_empty_in_window_returns_empty_string(tmp_path):
    path = tmp_path / "session.jsonl"
    _write_jsonl(path, [
        {"timestamp": "2026-01-01T00:00:00Z",
         "message": {"role": "user", "content": "old"}},
    ])
    since = datetime(2026, 1, 2, tzinfo=timezone.utc)
    assert _compress_jsonl(path, since=since) == ""


def test_compress_jsonl_missing_file_returns_empty_string(tmp_path):
    assert _compress_jsonl(tmp_path / "nope.jsonl") == ""


def test_compress_jsonl_skips_malformed_lines(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text(
        'not json at all\n'
        + json.dumps({"message": {"role": "user", "content": "still works"}}) + "\n",
        encoding="utf-8",
    )
    out = _compress_jsonl(path)
    assert "still works" in out


# ---------------------------------------------------------------------------
# Codex rollout transcripts (_compress_codex_jsonl)
# ---------------------------------------------------------------------------

def test_compress_codex_jsonl_extracts_user_and_assistant_text(tmp_path):
    path = tmp_path / "rollout-2026-01-02T00-00-00-abc.jsonl"
    _write_jsonl(path, [
        {"type": "session_meta", "timestamp": "2026-01-02T00:00:00Z",
         "payload": {"cwd": "/home/me/project"}},
        {"type": "response_item", "timestamp": "2026-01-02T00:01:00Z",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "what broke the build?"}]}},
        {"type": "response_item", "timestamp": "2026-01-02T00:02:00Z",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "A missing dependency."}]}},
    ])
    out = _compress_codex_jsonl(path)
    assert "(project: /home/me/project)" in out
    assert "USER: what broke the build?" in out
    assert "ASSISTANT: A missing dependency." in out


def test_compress_codex_jsonl_reads_cwd_from_turn_context(tmp_path):
    path = tmp_path / "rollout.jsonl"
    _write_jsonl(path, [
        {"type": "turn_context", "timestamp": "2026-01-02T00:00:00Z",
         "payload": {"TurnEnvironmentSelections": {"cwd": "/home/me/other-project"}}},
        {"type": "response_item", "timestamp": "2026-01-02T00:01:00Z",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "hi"}]}},
    ])
    out = _compress_codex_jsonl(path)
    assert "(project: /home/me/other-project)" in out


def test_compress_codex_jsonl_ignores_non_message_response_items(tmp_path):
    path = tmp_path / "rollout.jsonl"
    _write_jsonl(path, [
        {"type": "response_item", "timestamp": "2026-01-02T00:00:00Z",
         "payload": {"type": "function_call", "name": "shell", "arguments": "{}"}},
        {"type": "event_msg", "timestamp": "2026-01-02T00:00:01Z",
         "payload": {"type": "agent_reasoning", "text": "thinking..."}},
        {"type": "response_item", "timestamp": "2026-01-02T00:00:02Z",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "only this counts"}]}},
    ])
    out = _compress_codex_jsonl(path)
    assert "only this counts" in out
    assert "thinking" not in out
    assert "shell" not in out


def test_compress_codex_jsonl_respects_since_window(tmp_path):
    path = tmp_path / "rollout.jsonl"
    _write_jsonl(path, [
        {"type": "response_item", "timestamp": "2026-01-01T00:00:00Z",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "old message"}]}},
        {"type": "response_item", "timestamp": "2026-01-02T12:00:00Z",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "new message"}]}},
    ])
    since = datetime(2026, 1, 2, tzinfo=timezone.utc)
    out = _compress_codex_jsonl(path, since=since)
    assert "new message" in out
    assert "old message" not in out


def test_compress_codex_jsonl_unrecognized_record_shape_is_skipped_not_raised(tmp_path):
    path = tmp_path / "rollout.jsonl"
    _write_jsonl(path, [
        {"type": "some_future_record_type", "timestamp": "2026-01-02T00:00:00Z",
         "payload": {"weird": "shape", "no": ["message", "here"]}},
        {"type": "response_item", "timestamp": "2026-01-02T00:00:01Z",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "still parses"}]}},
    ])
    out = _compress_codex_jsonl(path)
    assert "still parses" in out


def test_compress_codex_jsonl_empty_in_window_returns_empty_string(tmp_path):
    path = tmp_path / "rollout.jsonl"
    _write_jsonl(path, [
        {"type": "response_item", "timestamp": "2026-01-01T00:00:00Z",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "old"}]}},
    ])
    since = datetime(2026, 1, 2, tzinfo=timezone.utc)
    assert _compress_codex_jsonl(path, since=since) == ""


def test_compress_codex_jsonl_missing_file_returns_empty_string(tmp_path):
    assert _compress_codex_jsonl(tmp_path / "nope.jsonl") == ""
