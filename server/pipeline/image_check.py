"""Post-render QA for the text an image model actually put in a scene.

Replaces the old "no text anywhere" brief rule (deleted from draft.py's
IMAGE_BRIEF_PROMPT — see ROADMAP.md Phase 9): that rule was wrong on the
merits (the best-performing post of the month had two clean words on a box)
and was ignored roughly half the time anyway. Text presence isn't the
problem; broken text is. This checks the rendered output instead of asking
the brief to ban something it can't reliably suppress.

Uses run_llm (the same Claude/Codex text+vision path digest.py's
describe_screenshots() already uses), not a dedicated vision API — one fewer
dependency and it stays on whatever backend the rest of the pipeline runs.

Calibration record, from four renders pulled from the LinkedIn export behind
the Phase 9 analysis:
- a screen mockup reading "PLATFORM DATA", "REAL OUTCOMES", a form label
  rendered as scene content, and a storefront sign reading "STORESTONE"
  -> must FAIL (a nonsense brand name plus a legible dashboard)
- clock face numerals -> must PASS
- a small logo on a switch -> must PASS
- "OPEN SOURCE" on a cardboard box -> must PASS (two correctly spelled words
  that belong in the scene; this was the best-performing post of the month)
"""
from __future__ import annotations

from pathlib import Path

from ..util import get_logger
from .claude_cli import extract_json
from .llm import run_llm

log = get_logger("pipeline.image_check")

TEXT_CHECK_PROMPT = """Look at the image below (available via the Read tool, or
attached directly). Judge only the rendered text and interface-like content in
it — not the composition, subject, or style.

Flag it if either is true:
1. Any word is misspelled, invented, or made of malformed/half-formed
   letterforms (doubled letters, garbled glyphs, a brand name that doesn't
   look like a real word).
2. There is legible interface content: a dashboard, chart, slide, form, or
   document with enough readable text to read as a screen mockup.

Do NOT flag a short, correctly spelled word or phrase that belongs naturally
in the scene (a label on a box, a sign, a logo), or incidental numerals on a
real object (a clock face, a keyboard, a dial).

Image: {path}

Return ONLY a JSON object, no other text:
{{"malformed": true or false, "interface_text": true or false,
  "note": "one short phrase naming the problem word or element, empty string\
 if none"}}
"""


def check_image_text(cfg, image_path: str | Path) -> tuple[bool, str]:
    """(problem_found, what) — one cheap vision call. Never raises.

    A checker failure (timeout, bad JSON, model refusal...) must never cost a
    post, so any exception here is logged and treated as "no problem found" —
    the image is delivered exactly as if the check had passed.
    """
    path = str(image_path)
    model = str(cfg.get("image.text_check_model", "") or "").strip() or None
    prompt = TEXT_CHECK_PROMPT.format(path=path)
    try:
        result = run_llm(cfg, prompt, allow_read_dirs=[str(Path(path).parent)],
                         images=[path], timeout=120, model=model).text
        data = extract_json(result)
    except Exception as e:
        log.warning("image text check failed (%s) — keeping the image", e)
        return False, ""
    malformed = bool(data.get("malformed"))
    interface_text = bool(data.get("interface_text"))
    note = str(data.get("note") or "").strip()
    return (malformed or interface_text), note
