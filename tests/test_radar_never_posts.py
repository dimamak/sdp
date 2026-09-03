"""Static guard: no radar module may reach XClient.post — plan.md §11.

Import-based, not mock-based: the only way to pass this is for a radar module
to genuinely not import x_client or call a method literally named `.post`.
Mocking XClient in a runtime test could be defeated by a module that reaches
it some other way; walking the AST can't be.
"""
import ast
from pathlib import Path

RADAR_DIR = Path(__file__).resolve().parent.parent / "server" / "radar"


def _radar_modules() -> list[Path]:
    mods = sorted(RADAR_DIR.glob("*.py"))
    assert mods, f"no radar modules found under {RADAR_DIR} — is the path right?"
    return mods


def test_no_radar_module_imports_x_client():
    for path in _radar_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and "x_client" in node.module:
                raise AssertionError(f"{path.name} imports x_client ({node.module})")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "x_client" not in alias.name, f"{path.name} imports x_client"


def test_no_radar_module_calls_dot_post():
    """Belt-and-braces: even without importing x_client by name, no radar
    module should call a method literally named `.post(` — the one call
    XClient exposes for actually publishing to X."""
    for path in _radar_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "post":
                raise AssertionError(f"{path.name}:{node.lineno} calls .post(...)")
