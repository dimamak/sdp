"""The heartbeat is how the doctor tells a working laptop install from one where
the bot was closed weeks ago — the two are otherwise indistinguishable from the
config alone.
"""
import time

from server.bot.scheduler import (DEFAULT_POLL_SECONDS, heartbeat, heartbeat_path)
from server.config import Config


def _cfg(tmp_path) -> Config:
    return Config({"store_dir": str(tmp_path)}, {}, tmp_path / "config.yaml")


def test_no_heartbeat_means_the_bot_has_never_run_here(tmp_path):
    assert heartbeat(_cfg(tmp_path)) is None


def test_a_fresh_beat_reports_its_age_and_the_promised_interval(tmp_path):
    cfg = _cfg(tmp_path)
    heartbeat_path(cfg).write_text(f"{int(time.time())} 120\n", encoding="utf-8")
    age, interval = heartbeat(cfg)
    assert age < 5 and interval == 120


def test_the_interval_is_read_from_the_file_not_assumed(tmp_path):
    # a bot started with a non-default poll must not read as dead
    cfg = _cfg(tmp_path)
    heartbeat_path(cfg).write_text(f"{int(time.time())} 3600\n", encoding="utf-8")
    assert heartbeat(cfg)[1] == 3600


def test_an_unreadable_beat_falls_back_to_the_default_interval(tmp_path):
    cfg = _cfg(tmp_path)
    heartbeat_path(cfg).write_text("garbage\n", encoding="utf-8")
    age, interval = heartbeat(cfg)
    assert interval == DEFAULT_POLL_SECONDS
    assert age < 5


def test_an_old_beat_reports_its_real_age(tmp_path):
    import os

    cfg = _cfg(tmp_path)
    p = heartbeat_path(cfg)
    p.write_text(f"0 {DEFAULT_POLL_SECONDS}\n", encoding="utf-8")
    old = time.time() - 86400
    os.utime(p, (old, old))
    age, _ = heartbeat(cfg)
    assert age > 86000
