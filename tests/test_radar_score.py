from datetime import datetime, timedelta, timezone

from server.config import Config
from server.radar.score import age_minutes, is_expired, score_post

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def _cfg(overrides=None):
    return Config({"radar": overrides or {}}, {}, None)


def _post(**kw):
    base = {
        "id": "1", "author_handle": "author", "text": "hello",
        "created_at": (NOW - timedelta(minutes=10)).isoformat(),
        "views": 5000, "likes": 50, "reposts": 5, "replies": 3,
        "is_own": False, "is_reply": False, "is_repost": False,
    }
    base.update(kw)
    return base


def test_own_post_is_gated():
    r = score_post(_post(is_own=True), _cfg(), now=NOW)
    assert r.gate == "own_post"
    assert r.score == 0.0


def test_reply_post_is_gated():
    r = score_post(_post(is_reply=True), _cfg(), now=NOW)
    assert r.gate == "is_reply"


def test_repost_is_gated():
    r = score_post(_post(is_repost=True), _cfg(), now=NOW)
    assert r.gate == "is_repost"


def test_too_new_is_gated():
    post = _post(created_at=(NOW - timedelta(minutes=1)).isoformat())
    assert score_post(post, _cfg(), now=NOW).gate == "too_new"


def test_too_old_is_gated():
    post = _post(created_at=(NOW - timedelta(minutes=30)).isoformat())
    assert score_post(post, _cfg(), now=NOW).gate == "too_old"


def test_low_views_is_gated():
    assert score_post(_post(views=10), _cfg(), now=NOW).gate == "low_reach"


def test_too_many_replies_is_gated():
    assert score_post(_post(replies=100), _cfg(), now=NOW).gate == "crowded"


def test_gates_are_checked_independently_first_failure_wins():
    # too-new AND low-views: age is checked before views, so too_new wins —
    # not a coincidence of the two failures cancelling out
    post = _post(created_at=(NOW - timedelta(minutes=1)).isoformat(), views=1)
    assert score_post(post, _cfg(), now=NOW).gate == "too_new"


def test_passing_post_scores_from_first_sighting_alone():
    r = score_post(_post(), _cfg(), now=NOW)
    assert r.gate is None
    assert 0.0 < r.score <= 1.0


def test_more_views_per_minute_scores_higher():
    low = score_post(_post(views=600), _cfg(), now=NOW)
    high = score_post(_post(views=50000), _cfg(), now=NOW)
    assert high.score > low.score


def test_baseline_and_velocity_are_optional_bonuses_not_required():
    plain = score_post(_post(), _cfg(), now=NOW)
    with_bonus = score_post(
        _post(author_baseline_reach_rate=1000, velocity_views_per_min=1000), _cfg(), now=NOW)
    assert plain.gate is None
    assert with_bonus.gate is None
    assert 0.0 <= plain.score <= 1.0
    assert 0.0 <= with_bonus.score <= 1.0


def test_relevance_contributes_to_the_final_score():
    no_rel = score_post(_post(), _cfg(), relevance=0.0, now=NOW)
    with_rel = score_post(_post(), _cfg(), relevance=1.0, now=NOW)
    assert with_rel.score > no_rel.score


def test_custom_weights_are_respected():
    cfg = _cfg({"weights": {"reach": 0, "quality": 0, "crowdedness": 0, "relevance": 1}})
    r = score_post(_post(), cfg, relevance=0.5, now=NOW)
    assert r.score == 0.5


def test_age_minutes_handles_z_and_offset_timestamps_the_same():
    aware = _post(created_at="2026-09-01T11:50:00+00:00")
    zulu = _post(created_at="2026-09-01T11:50:00Z")
    assert age_minutes(aware, NOW) == age_minutes(zulu, NOW)


def test_is_expired_matches_the_max_age_gate():
    cfg = _cfg({"max_age_minutes": 25})
    fresh = _post(created_at=(NOW - timedelta(minutes=10)).isoformat())
    stale = _post(created_at=(NOW - timedelta(minutes=30)).isoformat())
    assert not is_expired(fresh, cfg, now=NOW)
    assert is_expired(stale, cfg, now=NOW)
