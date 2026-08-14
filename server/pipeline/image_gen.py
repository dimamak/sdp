"""Post illustrations via the Gemini API (nano banana).

Called by the bot after you tap Approve, never by the nightly run — a render
costs money and only happens for a post you already said yes to.

Uses the `google-genai` SDK rather than raw requests (the house style elsewhere)
because the image response envelope moves faster than the docs: Google now
recommends the Interactions API, but its wire schema was already replaced once
and needs google-genai>=2.0.0, while `models.generate_content` keeps working.
The SDK call lives in _render() alone, so switching is a local change.

Note the model returns JPEG regardless of what you ask for — hence .jpg
everywhere downstream, including the content type LinkedIn is handed.

Smoke test:
    python -m server.pipeline.image_gen "PROMPT" [--out PATH] [--aspect 1:1] [--size 2K]
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from ..util import get_logger

log = get_logger("pipeline.image")

MIN_DEADLINE = 10               # the API rejects a shorter one outright: "Manually set
                                # deadline 5s is too short. Minimum allowed deadline is 10s."
DEFAULT_MODEL = "gemini-3-pro-image"
DEFAULT_ASPECT = "1:1"          # square wins the most feed height on mobile
DEFAULT_SIZE = "2K"             # ~1.5-2 MB JPEG: fine for Telegram (10 MB) and LinkedIn
EXT_BY_MIME = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}

_client = None                  # the bot runs for weeks; build the client once


class ImageGenError(RuntimeError):
    """A render that failed in a way worth explaining to the user.

    reason ∈ config | auth | quota | safety | no_image | http | timeout
    detail carries the model's or API's own words — for a safety refusal that is
    the whole point, since it tells you what to say in your next reply.
    """

    def __init__(self, message: str, *, reason: str, detail: str = ""):
        super().__init__(message)
        self.reason = reason
        self.detail = detail


@dataclass
class GeneratedImage:
    path: Path
    mime: str
    size_bytes: int
    model: str
    prompt: str
    text_notes: str


def _get_client(cfg):
    global _client
    if _client is None:
        key = cfg.secret("GEMINI_API_KEY")
        if not key:
            raise ImageGenError(
                "GEMINI_API_KEY is not set - run: python -m setup.wizard --source image",
                reason="config")
        try:
            from google import genai
        except ImportError as e:
            raise ImageGenError(
                "google-genai is not installed — run: pip install -r requirements.txt",
                reason="config", detail=str(e)) from e
        _client = genai.Client(api_key=key)
    return _client


def _render(client, model: str, prompt: str, aspect_ratio: str, image_size: str,
            timeout: int) -> tuple[bytes, str, str]:
    """One API call. Returns (image_bytes, mime, text_notes).

    The ONLY place the SDK call shape lives — see the module docstring.
    TEXT is always requested alongside IMAGE: a refusal comes back as text, and
    passing it through is what makes the failure actionable.
    """
    from google.genai import types

    resp = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_modalities=["TEXT", "IMAGE"],
            image_config=types.ImageConfig(aspect_ratio=aspect_ratio,
                                           image_size=image_size),
            http_options=types.HttpOptions(timeout=timeout * 1000),  # milliseconds
        ),
    )

    raw, mime, notes = None, "image/jpeg", []
    for cand in (resp.candidates or []):
        for part in ((cand.content.parts if cand.content else None) or []):
            blob = getattr(part, "inline_data", None)
            if blob is not None and blob.data and str(blob.mime_type or "").startswith("image/"):
                raw, mime = blob.data, blob.mime_type
            elif getattr(part, "text", None):
                notes.append(part.text)

    if raw is None:
        finish = str(getattr(resp.candidates[0], "finish_reason", "") if resp.candidates else "")
        blocked = getattr(getattr(resp, "prompt_feedback", None), "block_reason", None)
        detail = (" ".join(notes).strip() or str(blocked or "") or finish)[:500]
        if blocked or "SAFETY" in finish.upper() or "PROHIBITED" in finish.upper():
            raise ImageGenError("the model declined to illustrate this",
                                reason="safety", detail=detail)
        raise ImageGenError("the model returned no image", reason="no_image", detail=detail)

    return raw, mime, " ".join(notes).strip()


def strip_content_credentials(data: bytes) -> bytes:
    """Drop the C2PA provenance metadata from a JPEG.

    Gemini attaches Content Credentials as an APP11 JUMBF segment; LinkedIn reads
    it and renders the "Cr" badge over the image. This removes that segment only —
    tables and the compressed scan are copied verbatim, so it is lossless and needs
    no image library. Anything unexpected in the structure returns the input
    unchanged rather than risking a corrupt file.

    The SynthID watermark lives in the pixels, not the metadata, and is unaffected:
    this removes the visible disclosure, not the detectability.
    """
    if not data.startswith(b"\xff\xd8"):
        return data
    out, i, n = bytearray(data[:2]), 2, len(data)
    try:
        while i < n:
            if data[i] != 0xFF:
                return data                      # not on a marker boundary — bail
            m = data[i + 1]
            if m == 0xDA:                        # start of scan: copy the rest as-is
                out += data[i:]
                return bytes(out)
            if m == 0xD9 or 0xD0 <= m <= 0xD7:
                out += data[i:i + 2]
                i += 2
                continue
            length = int.from_bytes(data[i + 2:i + 4], "big")
            if length < 2 or i + 2 + length > n:
                return data
            if m != 0xEB:                        # keep everything except APP11
                out += data[i:i + 2 + length]
            i += 2 + length
    except IndexError:
        return data
    return bytes(out)


def _classify(e: Exception) -> ImageGenError:
    """Map an SDK exception onto something the bot can show and act on."""
    from google.genai import errors

    if isinstance(e, errors.APIError):
        code, msg = e.code, str(e.message or e)
        # a rejected key comes back as a plain 400, not a 401 — without this it
        # would look retryable and burn three attempts on a typo
        if code in (401, 403) or "api key not valid" in msg.lower():
            return ImageGenError("Gemini rejected the API key", reason="auth", detail=msg[:500])
        if code == 429:
            return ImageGenError("Gemini quota exhausted", reason="quota", detail=msg[:500])
        if code == 404:
            return ImageGenError("model not found - check image.model", reason="config",
                                 detail=msg[:500])
        if code and code < 500:
            return ImageGenError(f"Gemini rejected the request ({code})",
                                 reason="http", detail=msg[:500])
        return ImageGenError(f"Gemini server error ({code})", reason="http", detail=msg[:500])
    return ImageGenError(f"image generation failed: {e}", reason="http", detail=str(e)[:500])


def generate_image(cfg, prompt: str, *, out_path: Path, aspect_ratio: str | None = None,
                   image_size: str | None = None, timeout: int | None = None) -> GeneratedImage:
    """Render `prompt` to `out_path` (extension corrected to match what came back).

    Takes cfg and plain values only — no Store — so it is safe to call from
    asyncio.to_thread. Raises ImageGenError on every failure path.
    """
    client = _get_client(cfg)
    model = str(cfg.get("image.model", DEFAULT_MODEL))
    aspect = aspect_ratio or str(cfg.get("image.aspect_ratio", DEFAULT_ASPECT))
    size = image_size or str(cfg.get("image.image_size", DEFAULT_SIZE))
    budget = max(int(timeout or cfg.get("image.timeout", 180)), MIN_DEADLINE)

    suffix = str(cfg.get("image.style_suffix", "") or "").strip()
    full_prompt = f"{prompt.strip()}\n\n{suffix}" if suffix else prompt.strip()

    deadline = time.monotonic() + budget
    attempt, last = 0, None
    while True:
        attempt += 1
        remaining = deadline - time.monotonic()
        # stop rather than send a deadline the API will reject — a sub-10s attempt
        # comes back as a 400 that looks like a retryable error and isn't
        if remaining < MIN_DEADLINE:
            raise ImageGenError(f"render timed out after {budget}s", reason="timeout",
                                detail=(last.detail if last else ""))
        try:
            raw, mime, notes = _render(client, model, full_prompt, aspect, size,
                                       int(remaining))
            break
        except ImageGenError:
            raise                                   # safety / no_image: retrying won't help
        except Exception as e:                      # noqa: BLE001 - classified below
            last = _classify(e)
            # a bad key or a bad model name is not going to fix itself
            if last.reason in ("auth", "config") or attempt > 3:
                raise last from e
            backoff = min(20 if last.reason == "quota" else 2 ** attempt,
                          max(deadline - time.monotonic(), 0))
            if backoff <= 0:
                raise last from e
            log.warning("render attempt %d failed (%s), retrying in %.0fs",
                        attempt, last.reason, backoff)
            time.sleep(backoff)

    if mime == "image/jpeg" and cfg.get("image.strip_content_credentials", True):
        stripped = strip_content_credentials(raw)
        if len(stripped) < len(raw):
            log.info("stripped %d bytes of content credentials", len(raw) - len(stripped))
            raw = stripped

    out_path = out_path.with_suffix("." + EXT_BY_MIME.get(mime, "jpg"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(raw)
    log.info("rendered %s (%s, %d bytes, %s %s)", out_path, mime, len(raw), aspect, size)
    return GeneratedImage(path=out_path, mime=mime, size_bytes=len(raw), model=model,
                          prompt=full_prompt, text_notes=notes)


def main(argv=None) -> int:
    import argparse

    from ..config import Config

    ap = argparse.ArgumentParser(description="Render one image and write it to disk.")
    ap.add_argument("prompt")
    ap.add_argument("--out", default="image-smoke.jpg")
    ap.add_argument("--aspect", default=None)
    ap.add_argument("--size", default=None)
    ap.add_argument("--config", default=None)
    args = ap.parse_args(argv)

    cfg = Config.load(args.config)
    try:
        img = generate_image(cfg, args.prompt, out_path=Path(args.out),
                             aspect_ratio=args.aspect, image_size=args.size)
    except ImageGenError as e:
        print(f"FAILED [{e.reason}] {e}")
        if e.detail:
            print(f"  {e.detail}")
        return 1
    print(f"{img.path}  {img.mime}  {img.size_bytes:,} bytes  model={img.model}")
    if img.text_notes:
        print(f"  notes: {img.text_notes[:500]}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
