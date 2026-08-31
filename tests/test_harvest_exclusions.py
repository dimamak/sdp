"""Covers server/harvest/claude_common.py's harvest_dir() exclusions.

The load-bearing guard here is the one that keeps the nightly digest from eating
its own tail: `own_project_dirname()` names the project dir Claude Code assigns to
the pipeline's own drafting sessions, and `harvest_dir()` always excludes it — plus
any `exclude_projects` globs — so last night's drafting conversation never gets
harvested back into tonight's digest. Nothing in tests/ used to cover any of this,
so a silent regression would only show up as increasingly weird drafts.
"""
import json
import os
from datetime import datetime, timedelta, timezone

from server.config import Config
from server.harvest.claude_common import harvest_dir, own_project_dirname
from server.store import Store


def _cfg(tmp_path):
    return Config({"store_dir": str(tmp_path / "store")}, {}, tmp_path / "config.yaml")


def _setup(tmp_path, tree):
    projects = tmp_path / "projects"
    projects.mkdir()
    for rel, text in tree.items():
        p = projects / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    cfg = _cfg(tmp_path)
    return projects, cfg, Store(cfg.path_of("store_dir"))


def _since(hours_ago=1):
    return datetime.now(timezone.utc) - timedelta(hours=hours_ago)


def _age(projects, rel, hours_ago):
    old = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).timestamp()
    os.utime(projects / rel, (old, old))


def _session_ids(store):
    return {json.loads(r["meta_json"])["session_id"]
            for r in store.db.execute("SELECT meta_json FROM items")}


# ---------------------------------------------------------------------------
# exclusions — the digest must never eat its own tail
# ---------------------------------------------------------------------------

def test_own_project_dir_is_skipped(tmp_path):
    own = own_project_dirname()
    projects, cfg, store = _setup(tmp_path, {
        f"{own}/drafting.jsonl": '{"a": 1}\n',
        "real-work/work.jsonl": '{"a": 1}\n',
    })
    assert harvest_dir(projects, _since(), "claude", cfg, store) == 1
    assert _session_ids(store) == {"work"}


def test_exclude_projects_glob_is_skipped(tmp_path):
    projects, cfg, store = _setup(tmp_path, {
        "secretproj/secret.jsonl": '{"a": 1}\n',
        "public/public.jsonl": '{"a": 1}\n',
    })
    n = harvest_dir(projects, _since(), "claude", cfg, store,
                    exclude_projects=["secret*"])
    assert n == 1
    assert _session_ids(store) == {"public"}


# ---------------------------------------------------------------------------
# normal registration
# ---------------------------------------------------------------------------

def test_normal_jsonl_is_registered_exactly_once(tmp_path):
    projects, cfg, store = _setup(tmp_path, {"proj/session.jsonl": '{"a": 1}\n'})
    assert harvest_dir(projects, _since(), "claude", cfg, store) == 1
    assert len(_session_ids(store)) == 1


# ---------------------------------------------------------------------------
# since / session_ids filters
# ---------------------------------------------------------------------------

def test_file_older_than_since_is_skipped(tmp_path):
    projects, cfg, store = _setup(tmp_path, {"proj/old.jsonl": '{"a": 1}\n'})
    _age(projects, "proj/old.jsonl", hours_ago=5)
    assert harvest_dir(projects, _since(hours_ago=2), "claude", cfg, store) == 0
    assert _session_ids(store) == set()


def test_session_ids_only_takes_listed_stems(tmp_path):
    projects, cfg, store = _setup(tmp_path, {
        "proj/aaa.jsonl": '{"a": 1}\n',
        "proj/bbb.jsonl": '{"a": 1}\n',
    })
    n = harvest_dir(projects, _since(), "claude", cfg, store, session_ids={"aaa"})
    assert n == 1
    assert _session_ids(store) == {"aaa"}


# ---------------------------------------------------------------------------
# dedup — re-runs are idempotent, but growth re-registers
# ---------------------------------------------------------------------------

def test_rerun_does_not_double_register_but_grew_file_does(tmp_path):
    projects, cfg, store = _setup(tmp_path, {"proj/session.jsonl": "x"})
    assert harvest_dir(projects, _since(), "claude", cfg, store) == 1
    assert harvest_dir(projects, _since(), "claude", cfg, store) == 0

    (projects / "proj/session.jsonl").write_text("xy", encoding="utf-8")
    assert harvest_dir(projects, _since(), "claude", cfg, store) == 1
    rows = store.db.execute("SELECT external_id FROM items").fetchall()
    assert [r["external_id"] for r in rows] == ["session:1", "session:2"]
