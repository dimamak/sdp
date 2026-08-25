from server.pipeline.lock import nightly_lock


def test_first_instance_acquires_lock(tmp_path):
    lock_path = tmp_path / ".nightly.lock"
    with nightly_lock(lock_path) as acquired:
        assert acquired is True


def test_second_concurrent_instance_is_refused(tmp_path):
    lock_path = tmp_path / ".nightly.lock"
    with nightly_lock(lock_path) as first:
        assert first is True
        with nightly_lock(lock_path) as second:
            assert second is False


def test_lock_is_released_for_the_next_run(tmp_path):
    lock_path = tmp_path / ".nightly.lock"
    with nightly_lock(lock_path) as first:
        assert first is True
    with nightly_lock(lock_path) as second:
        assert second is True


def test_lock_creates_parent_directory(tmp_path):
    lock_path = tmp_path / "nested" / "dir" / ".nightly.lock"
    with nightly_lock(lock_path) as acquired:
        assert acquired is True
    assert lock_path.exists()
