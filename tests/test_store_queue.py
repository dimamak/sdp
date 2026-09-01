from datetime import datetime, timedelta, timezone

from server.store import Store


def _store(tmp_path) -> Store:
    return Store(tmp_path / "store")


def test_due_drafts_respects_scheduled_at_and_ignores_non_queued_statuses(tmp_path):
    store = _store(tmp_path)
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

    due_id = store.add_draft("2026-09-07", "due post")
    store.queue_draft(due_id, past)

    future_id = store.add_draft("2026-09-07", "not due yet")
    store.queue_draft(future_id, future)

    never_queued_id = store.add_draft("2026-09-07", "still pending")
    assert never_queued_id  # sanity: exists, but never touched by due_drafts below

    posted_id = store.add_draft("2026-09-07", "already posted")
    store.update_draft(posted_id, status="posted", scheduled_at=past)

    due = {r["id"] for r in store.due_drafts(datetime.now(timezone.utc).isoformat())}
    assert due == {due_id}


def test_queue_draft_preserves_the_chosen_image_instead_of_discarding_it(tmp_path):
    store = _store(tmp_path)
    draft_id = store.add_draft("2026-09-07", "post with image")
    img_id = store.add_image(draft_id, 1, path="/tmp/x.png", status="pending_review")

    store.queue_draft(draft_id, datetime.now(timezone.utc).isoformat(), image_id=img_id)

    row = store.get_draft(draft_id)
    assert row["status"] == "queued"
    assert int(row["scheduled_image_id"]) == img_id
    # the image row itself is untouched by queue_draft — still fetchable, not discarded
    img = store.image_by_id(img_id)
    assert img["status"] == "pending_review"
    assert img["path"] == "/tmp/x.png"


def test_expire_draft_sets_expired_status(tmp_path):
    store = _store(tmp_path)
    draft_id = store.add_draft("2026-09-07", "stale")
    store.queue_draft(draft_id, datetime.now(timezone.utc).isoformat())

    store.expire_draft(draft_id)

    assert store.get_draft(draft_id)["status"] == "expired"


def test_queued_drafts_lists_only_queued_ordered_by_slot(tmp_path):
    store = _store(tmp_path)
    later = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat()
    sooner = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

    later_id = store.add_draft("2026-09-07", "later")
    store.queue_draft(later_id, later)
    sooner_id = store.add_draft("2026-09-07", "sooner")
    store.queue_draft(sooner_id, sooner)
    store.add_draft("2026-09-07", "untouched")

    assert [r["id"] for r in store.queued_drafts()] == [sooner_id, later_id]
