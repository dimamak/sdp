from datetime import datetime, timedelta, timezone

from server.config import Config
from server.radar import poll, watchlist
from server.store import Store

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _cfg(**radar_over):
    radar = {"monthly_budget_usd": 5.0, "watchlist": [], "watchlist_max": None,
             "growth": {"min_observations": 3, "min_reach_rate": 200,
                        "min_topic_overlap": 0.0, "swap_margin": 0.25,
                        "grace_days": 14, "repropose_days": 30,
                        "max_watchlist_prompts_per_day": 1}}
    radar.update(radar_over)
    return Config({"radar": radar}, {}, None)


def _post(author_id="a1", handle="author", views=3000, replies=2, age_min=10, text="hello world"):
    created = (NOW - timedelta(minutes=age_min)).isoformat()
    return {"id": "1", "author_id": author_id, "author_handle": handle, "text": text,
            "created_at": created, "views": views, "likes": 0, "reposts": 0,
            "replies": replies, "is_own": False, "is_reply": False, "is_repost": False}


def _sight(cfg, store, n=3, **post_kw):
    for _ in range(n):
        watchlist.record_sighting(cfg, store, _post(**post_kw), NOW)


def test_lane_b_polls_with_exactly_one_watchlist_entry(tmp_path, monkeypatch):
    store = Store(tmp_path / "store")
    store.upsert_radar_author("a1", handle="solo", on_watchlist=1,
                              watchlist_added_at=NOW.isoformat())
    cfg = _cfg(watchlist=[])

    def boom(*a, **kw):
        raise AssertionError("credentials aren't configured in this test")
    monkeypatch.setattr(poll.requests, "get", boom)
    # No X credentials configured -> poll_once degrades to [] rather than
    # calling the network, but effective_watchlist must still resolve to the
    # one grown entry (proves Lane B would poll it once credentials exist).
    assert watchlist.effective_watchlist(cfg, store) == ["solo"]
    assert poll.poll_once(cfg, store, now=NOW) == []


def test_candidate_needs_min_observations(tmp_path):
    store = Store(tmp_path / "store")
    cfg = _cfg()
    _sight(cfg, store, n=2)  # below min_observations=3
    assert watchlist.evaluate(cfg, store, NOW) is None
    _sight(cfg, store, n=1)  # now at 3
    proposal = watchlist.evaluate(cfg, store, NOW)
    assert proposal is not None
    assert proposal.kind == "add"
    assert proposal.candidate["author_id"] == "a1"


def test_reach_rate_floor_blocks_a_weak_candidate(tmp_path):
    store = Store(tmp_path / "store")
    cfg = _cfg()
    _sight(cfg, store, n=3, views=100, age_min=10)  # 10 views/min, floor is 200
    assert watchlist.evaluate(cfg, store, NOW) is None


def test_swap_requires_the_margin(tmp_path):
    store = Store(tmp_path / "store")
    cfg = _cfg(growth={"min_observations": 3, "min_reach_rate": 200, "min_topic_overlap": 0.0,
                       "swap_margin": 0.25, "grace_days": 0, "repropose_days": 30,
                       "max_watchlist_prompts_per_day": 1}, watchlist_max=1)
    added_at = (NOW - timedelta(days=30)).isoformat()
    store.upsert_radar_author("incumbent", handle="incumbent", on_watchlist=1,
                              watchlist_added_at=added_at, observations=5,
                              baseline_reach_rate=1000.0)
    # Candidate only 10% stronger -- short of the 25% margin.
    _sight(cfg, store, n=3, author_id="a1", handle="candidate", views=11000, age_min=10)
    assert watchlist.evaluate(cfg, store, NOW) is None

    # Now clearly stronger (>25%).
    store.set_radar_author("a1", observations_json=None, observations=0,
                           baseline_reach_rate=None)
    _sight(cfg, store, n=3, author_id="a1", handle="candidate", views=14000, age_min=10)
    proposal = watchlist.evaluate(cfg, store, NOW)
    assert proposal is not None
    assert proposal.kind == "replace"
    assert proposal.incumbent["author_id"] == "incumbent"
    assert proposal.candidate["author_id"] == "a1"


def test_grace_period_protects_a_new_incumbent(tmp_path):
    store = Store(tmp_path / "store")
    cfg = _cfg(growth={"min_observations": 3, "min_reach_rate": 200, "min_topic_overlap": 0.0,
                       "swap_margin": 0.25, "grace_days": 14, "repropose_days": 30,
                       "max_watchlist_prompts_per_day": 1}, watchlist_max=1)
    # Incumbent added yesterday -- well inside the 14-day grace period.
    store.upsert_radar_author("incumbent", handle="incumbent", on_watchlist=1,
                              watchlist_added_at=(NOW - timedelta(days=1)).isoformat(),
                              observations=5, baseline_reach_rate=100.0)
    _sight(cfg, store, n=3, author_id="a1", handle="candidate", views=100000, age_min=10)
    assert watchlist.evaluate(cfg, store, NOW) is None


def test_never_is_permanent(tmp_path):
    store = Store(tmp_path / "store")
    cfg = _cfg()
    _sight(cfg, store, n=3)
    watchlist.never(store, "a1")
    assert watchlist.evaluate(cfg, store, NOW) is None


def test_at_most_one_prompt_per_day(tmp_path):
    store = Store(tmp_path / "store")
    cfg = _cfg()
    _sight(cfg, store, n=3, author_id="a1", handle="one")
    _sight(cfg, store, n=3, author_id="a2", handle="two")
    proposal = watchlist.evaluate(cfg, store, NOW)
    assert proposal is not None
    watchlist.mark_proposed(store, proposal.candidate["author_id"], NOW)
    # A second candidate exists but the daily prompt budget is already spent.
    assert watchlist.evaluate(cfg, store, NOW) is None


def test_repropose_days_blocks_a_recently_declined_candidate(tmp_path):
    store = Store(tmp_path / "store")
    cfg = _cfg()
    _sight(cfg, store, n=3)
    watchlist.mark_proposed(store, "a1", NOW - timedelta(days=5))
    assert watchlist.evaluate(cfg, store, NOW) is None
    # Past repropose_days, the same author is eligible again.
    assert watchlist.evaluate(cfg, store, NOW + timedelta(days=26)) is not None


def test_add_that_would_exceed_budget_is_refused(tmp_path):
    store = Store(tmp_path / "store")
    # watchlist_max manually raised well above what the $1 budget actually
    # affords -- three existing members already cost ~$0.90/mo, so a fourth
    # (~$1.20/mo) must be refused even though there's cap headroom left.
    cfg = _cfg(monthly_budget_usd=1.0, watchlist_max=10)
    for i in range(3):
        store.upsert_radar_author(f"incumbent{i}", handle=f"incumbent{i}", on_watchlist=1,
                                  watchlist_added_at=NOW.isoformat())
    _sight(cfg, store, n=3)
    assert watchlist.evaluate(cfg, store, NOW) is None


def test_watchlist_max_derives_from_budget_when_unset(tmp_path):
    cfg = _cfg(monthly_budget_usd=5.0, watchlist_max=None)
    assert watchlist.watchlist_max(cfg) == 16

    cfg = _cfg(monthly_budget_usd=5.0, watchlist_max=3)
    assert watchlist.watchlist_max(cfg) == 3
