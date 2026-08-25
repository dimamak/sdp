import sys
from pathlib import Path

# Repo root must be importable as `server`/`setup` regardless of how pytest
# is invoked (bare `pytest`, `python -m pytest`, from a subdirectory, etc).
sys.path.insert(0, str(Path(__file__).resolve().parent))
