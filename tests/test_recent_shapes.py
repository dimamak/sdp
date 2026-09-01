from server.store import Store


def _store(tmp_path) -> Store:
    return Store(tmp_path / "store")


def test_recent_shapes_one_row_per_day_preferring_the_posted_draft(tmp_path):
    store = _store(tmp_path)
    # two drafts on the same day: an earlier skipped one and the one that got posted
    skipped_id = store.add_draft("2026-09-05", "Skipped take.\n")
    store.update_draft(skipped_id, status="skipped", shape="finding")
    posted_id = store.add_draft("2026-09-05", "Posted take?\n")
    store.update_draft(posted_id, status="posted", shape="ask")

    other_day_id = store.add_draft("2026-09-06", "Another day's post.\n")
    store.update_draft(other_day_id, shape="list")

    rows = store.recent_shapes(before_day="2026-09-07")

    by_day = {r["day"]: r for r in rows}
    assert set(by_day) == {"2026-09-05", "2026-09-06"}
    assert by_day["2026-09-05"]["shape"] == "ask"  # the posted one, not the skipped one
    assert by_day["2026-09-05"]["ended_with_question"] is True
    assert by_day["2026-09-06"]["shape"] == "list"


def test_recent_shapes_excludes_the_day_being_drafted(tmp_path):
    store = _store(tmp_path)
    store.add_draft("2026-09-07", "Today's own draft, not history yet.\n")
    rows = store.recent_shapes(before_day="2026-09-07")
    assert rows == []


def test_days_since_shape_returns_none_when_never_written(tmp_path):
    store = _store(tmp_path)
    store.add_draft("2026-09-05", "A finding post.\n")
    assert store.days_since_shape("ask", before_day="2026-09-07") is None


def test_days_since_shape_counts_calendar_days_since_the_last_one(tmp_path):
    store = _store(tmp_path)
    ask_id = store.add_draft("2026-09-01", "An ask post?\n")
    store.update_draft(ask_id, shape="ask")

    assert store.days_since_shape("ask", before_day="2026-09-07") == 6
