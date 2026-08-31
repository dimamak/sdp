import os
from pathlib import Path

from server.config import Config


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def test_load_explicit_path(tmp_path):
    cfg_path = _write(tmp_path / "config.yaml", "mode: laptop\npipeline:\n  backend: codex\n")
    cfg = Config.load(cfg_path)
    assert cfg.get("mode") == "laptop"
    assert cfg.get("pipeline.backend") == "codex"


def test_load_missing_file_yields_empty_config(tmp_path):
    cfg = Config.load(tmp_path / "does-not-exist.yaml")
    assert cfg.get("mode") is None
    assert cfg.get("mode", "server") == "server"


def test_env_var_points_at_config_path(tmp_path, monkeypatch):
    cfg_path = _write(tmp_path / "custom.yaml", "mode: server\n")
    monkeypatch.setenv("DAILYPOST_CONFIG", str(cfg_path))
    cfg = Config.load()
    assert cfg.get("mode") == "server"


def test_explicit_arg_overrides_env_var(tmp_path, monkeypatch):
    env_path = _write(tmp_path / "env.yaml", "mode: server\n")
    explicit_path = _write(tmp_path / "explicit.yaml", "mode: laptop\n")
    monkeypatch.setenv("DAILYPOST_CONFIG", str(env_path))
    cfg = Config.load(explicit_path)
    assert cfg.get("mode") == "laptop"


def test_dotted_get_missing_key_returns_default():
    cfg = Config({"pipeline": {"backend": "claude"}}, {}, None)
    assert cfg.get("pipeline.backend") == "claude"
    assert cfg.get("pipeline.missing", "fallback") == "fallback"
    assert cfg.get("not.even.a.dict.path") is None


def test_dotted_get_stops_at_non_dict_intermediate():
    cfg = Config({"pipeline": "not-a-dict"}, {}, None)
    assert cfg.get("pipeline.backend", "default") == "default"


def test_path_of_expands_user(tmp_path):
    cfg = Config({"store_dir": "~/dailypost-store"}, {}, tmp_path)
    p = cfg.path_of("store_dir")
    # path_of wraps expanduser's result in Path, which normalizes separators
    # (e.g. on Windows, expanduser leaves the un-expanded suffix's "/" as-is,
    # but Path(...) renders the whole thing with "\") — compare as Path, not
    # as a raw string, so the assertion doesn't depend on that normalization.
    assert p == Path(os.path.expanduser("~/dailypost-store"))


def test_path_of_missing_key_returns_none():
    cfg = Config({}, {}, None)
    assert cfg.path_of("store_dir") is None


def test_secret_reads_env_file(tmp_path):
    _write(tmp_path / "config.yaml", "mode: laptop\n")
    _write(tmp_path / ".env", 'TG_BOT_TOKEN="abc123"\nTG_ALLOWED_CHAT_ID=999\n')
    cfg = Config.load(tmp_path / "config.yaml")
    assert cfg.secret("TG_BOT_TOKEN") == "abc123"
    assert cfg.secret("TG_ALLOWED_CHAT_ID") == "999"
    assert cfg.secret("NOT_SET", "fallback") == "fallback"


def test_secret_real_environment_wins_over_env_file(tmp_path, monkeypatch):
    _write(tmp_path / "config.yaml", "mode: laptop\n")
    _write(tmp_path / ".env", "SOME_KEY=from_file\n")
    monkeypatch.setenv("SOME_KEY", "from_real_env")
    cfg = Config.load(tmp_path / "config.yaml")
    assert cfg.secret("SOME_KEY") == "from_real_env"


def test_sources_filters_disabled_by_default():
    cfg = Config({"sources": [
        {"type": "claude_sessions", "enabled": True},
        {"type": "gmail", "enabled": False},
        {"type": "telegram"},
    ]}, {}, None)
    types = [s["type"] for s in cfg.sources()]
    assert types == ["claude_sessions"]


def test_sources_only_enabled_false_returns_all():
    cfg = Config({"sources": [
        {"type": "claude_sessions", "enabled": True},
        {"type": "gmail", "enabled": False},
    ]}, {}, None)
    types = [s["type"] for s in cfg.sources(only_enabled=False)]
    assert types == ["claude_sessions", "gmail"]


def test_source_by_type_finds_disabled_sources_too():
    cfg = Config({"sources": [{"type": "gmail", "enabled": False}]}, {}, None)
    assert cfg.source_by_type("gmail") == {"type": "gmail", "enabled": False}
    assert cfg.source_by_type("missing") is None
