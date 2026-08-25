# Contributing

Thanks for considering a contribution.

## Getting set up

```bash
git clone <this repo> && cd <repo>
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/ -v
```

For end-to-end testing you'll want your own `config.yaml` in laptop mode —
see the [README quickstart](README.md#quickstart).
You do not need a server, a Telegram bot, or any paid API key to run the test
suite; those are only needed to exercise the pipeline live.

## Before opening a PR

- Run the test suite (`pytest tests/ -v`) and add tests for new behavior —
  see `tests/` for the existing fixture-based style (especially
  `tests/test_digest_compress.py`, which is the pattern to follow for parsing
  code).
- Keep secrets and personal paths out of commits. `config.yaml`, `.env`, and
  `*.session` files are git-ignored on purpose — don't force-add them.
- If you're touching the Codex JSONL parser (`server/pipeline/digest.py`'s
  `_compress_codex_jsonl` / `_codex_message_text`, or
  `server/harvest/codex_sessions.py`), remember the Codex rollout schema is
  undocumented and has already changed shape across releases. Parse
  defensively — an unrecognized record shape should be skipped, never raise.
- If you're touching `server/pipeline/lock.py` or anything path-related, keep
  it working on Windows, macOS, and Linux; CI runs the suite on all three.

## Reporting bugs / requesting features

Open a GitHub issue using the provided templates. For anything involving a
security or account-safety issue (credential handling, platform ban risk),
see [SECURITY.md](SECURITY.md) instead of a public issue.
