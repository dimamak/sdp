"""Static checks over extension/ (plan.md §13): the manifest lint and the
plain-Node selectors fixture. Neither needs a browser — the manifest is a
JSON file and selectors.test.js runs the actual selectors.js under plain
Node against a hand-rolled fake DOM (no jsdom/npm toolchain in this repo).
"""
import json
import shutil
import subprocess
from pathlib import Path

EXT_DIR = Path(__file__).resolve().parent.parent / "extension"


def _manifest() -> dict:
    return json.loads((EXT_DIR / "manifest.json").read_text(encoding="utf-8"))


def test_host_permissions_is_loopback_without_a_port():
    # Chrome match patterns reject ports outright, and a port here is exactly
    # what was shown (plan.md §3) to make Chrome preflight/CORS-block the
    # request instead of exempting it.
    assert _manifest()["host_permissions"] == ["http://127.0.0.1/*"]


def test_no_extension_code_sets_target_address_space():
    # Verified to be rejected outright when set from an extension origin
    # (plan.md §3) -- a future "fix" adding it back would break the path.
    # Matches the fetch-option usage ("targetAddressSpace: ...") rather than
    # the bare word, since this file's own docstrings mention it by name as
    # the thing never to do.
    for js_file in EXT_DIR.glob("*.js"):
        assert "targetAddressSpace:" not in js_file.read_text(encoding="utf-8"), js_file


def test_content_scripts_only_run_on_x():
    matches = _manifest()["content_scripts"][0]["matches"]
    assert matches
    assert all("twitter.com" in m or "x.com" in m for m in matches)


def test_manifest_is_mv3():
    assert _manifest()["manifest_version"] == 3


def test_selectors_js_fixture_passes_under_node():
    node = shutil.which("node")
    if node is None:
        import pytest
        pytest.skip("node not available in this environment")
    result = subprocess.run(  # noqa: S603
        [node, str(EXT_DIR / "selectors.test.js")],
        capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "all assertions passed" in result.stdout
