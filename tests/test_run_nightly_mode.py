"""Covers the laptop/server branch in server/pipeline/run_nightly.py:_run —
the gate that defers drafting until a laptop has checked in, which must be a
no-op in laptop-only mode (§2 of the open-sourcing plan: every source is
already local there, so nothing ever pushes a heartbeat).

The gate depends on the current wall-clock hour vs. a configured deadline
hour; instead of freezing time, tests use the boundary values 0 and 24 for
`wait_deadline_hour`, which make the "is it past the deadline" comparison
deterministic regardless of when the test runs.
"""
import argparse
from datetime import datetime, timedelta, timezone

from server.config import Config
from server.pipeline.run_nightly import _run, laptop_checked_in_since
from server.store import Store


def _cfg(tmp_path, data):
    base = {
        "store_dir": str(tmp_path / "store"),
        "ingest_dir": str(tmp_path / "ingest"),
        "sources": [],
    }
    base.update(data)
    return Config(base, {}, tmp_path / "config.yaml")


def _args(**kw):
    defaults = dict(dry_run=False, day=None, force=False, config=None)
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def _run_it(tmp_path, cfg_data, monkeypatch):
    """Runs _run() with build_digest replaced by a spy, so tests can tell
    whether the gate deferred (build_digest never reached) from whether it
    proceeded past the gate on an otherwise-empty store (build_digest reached,
    returns nothing to draft) — both cases return rc == 0, so rc alone can't
    distinguish them.
    """
    calls = []

    def fake_build_digest(cfg, store, day):
        calls.append(day)
        return "", []

    monkeypatch.setattr("server.pipeline.run_nightly.build_digest", fake_build_digest)

    cfg = _cfg(tmp_path, cfg_data)
    store = Store(cfg.path_of("store_dir"))
    rc = _run(cfg, _args())
    return rc, store, calls


# ---------------------------------------------------------------------------
# laptop_checked_in_since — the heartbeat check itself
# ---------------------------------------------------------------------------

def test_no_ingest_dir_means_not_checked_in(tmp_path):
    cfg = _cfg(tmp_path, {})
    assert laptop_checked_in_since(cfg, datetime.now(timezone.utc)) is False


def test_recent_heartbeat_counts_as_checked_in(tmp_path):
    ingest = tmp_path / "ingest"
    ingest.mkdir()
    (ingest / ".heartbeat-mylaptop").touch()
    cfg = _cfg(tmp_path, {})
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    assert laptop_checked_in_since(cfg, since) is True


def test_stale_heartbeat_does_not_count(tmp_path, monkeypatch):
    ingest = tmp_path / "ingest"
    ingest.mkdir()
    beat = ingest / ".heartbeat-mylaptop"
    beat.touch()
    old = (datetime.now(timezone.utc) - timedelta(days=2)).timestamp()
    import os
    os.utime(beat, (old, old))
    cfg = _cfg(tmp_path, {})
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    assert laptop_checked_in_since(cfg, since) is False


# ---------------------------------------------------------------------------
# _run's gate: mode == "laptop" must bypass it entirely, never deferring
# ---------------------------------------------------------------------------

def test_laptop_mode_never_defers_even_with_no_heartbeat(tmp_path, monkeypatch):
    # No ingest dir, no heartbeat, wait_for_laptop defaults True — in server
    # mode this would defer. In laptop mode it must proceed straight through.
    rc, store, calls = _run_it(tmp_path, {"mode": "laptop"}, monkeypatch)
    assert rc == 0
    assert calls, "build_digest should have been reached — laptop mode skips the gate"


def test_server_mode_defers_when_laptop_has_not_checked_in_and_before_deadline(tmp_path, monkeypatch):
    # deadline_hour=24: "local_now.hour < 24" is always true, so the deferral
    # branch is always taken here regardless of when the test runs.
    rc, store, calls = _run_it(tmp_path, {
        "mode": "server",
        "pipeline": {"wait_for_laptop": True, "wait_deadline_hour": 24},
    }, monkeypatch)
    assert rc == 0
    assert not calls, "build_digest should not run — the laptop hasn't checked in and it's before the deadline"


def test_server_mode_proceeds_past_deadline_even_without_heartbeat(tmp_path, monkeypatch):
    # deadline_hour=0: "local_now.hour < 0" is never true, so this always
    # takes the "past deadline, draft without the laptop" branch.
    rc, store, calls = _run_it(tmp_path, {
        "mode": "server",
        "pipeline": {"wait_for_laptop": True, "wait_deadline_hour": 0},
    }, monkeypatch)
    assert rc == 0
    assert calls, "build_digest should run once past the deadline, even with no heartbeat"


def test_server_mode_with_wait_disabled_never_defers(tmp_path, monkeypatch):
    rc, store, calls = _run_it(tmp_path, {
        "mode": "server",
        "pipeline": {"wait_for_laptop": False},
    }, monkeypatch)
    assert rc == 0
    assert calls, "build_digest should run — wait_for_laptop is disabled"
