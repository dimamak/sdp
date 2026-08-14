"""Nightly orchestrator: harvest → digest → draft → deliver.

Usage:
    python -m server.pipeline.run_nightly [--dry-run] [--config PATH] [--day YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import Config
from ..harvest import collect_all
from ..store import Store
from ..util import get_logger, target_day, window_start_iso
from .digest import build_digest
from .draft import deliver_drafts, notify, write_drafts
from .transcribe import transcribe_pending

log = get_logger("nightly")


def prune_old_files(cfg, store) -> None:
    days = int(cfg.get("retention_days", 30) or 0)
    if days <= 0 or not store.files_dir.exists():
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    import shutil
    for d in store.files_dir.iterdir():
        if d.is_dir() and d.name < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            log.info("pruned %s", d)


def prune_old_images(cfg, store) -> None:
    """Delete the bytes of old takes that were never published.

    Images attached to a posted draft are kept forever — they are the only local
    copy of published creative. The DB rows survive either way: the prompt history
    is what you want when the images start looking samey.
    """
    days = int(cfg.get("image.retention_days", cfg.get("retention_days", 30)) or 0)
    if days <= 0:
        return
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    n = 0
    for row in store.stale_unposted_images(cutoff):
        try:
            Path(row["path"]).unlink(missing_ok=True)
        except OSError as e:
            log.warning("could not remove %s: %s", row["path"], e)
            continue
        store.update_image(row["id"], path=None, status="pruned")
        n += 1
    if n:
        log.info("pruned %d unposted image(s)", n)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="harvest + digest only; no draft, no Telegram")
    ap.add_argument("--config", default=None)
    ap.add_argument("--day", default=None, help="override target day (YYYY-MM-DD)")
    args = ap.parse_args(argv)

    cfg = Config.load(args.config)
    store = Store(cfg.path_of("store_dir"))
    day = args.day or target_day(cfg)
    since = datetime.fromisoformat(window_start_iso(cfg))

    log.info("nightly run for %s (window since %s)", day, since)
    results = collect_all(cfg, store, since)
    for name, n in results.items():
        log.info("harvest %s: %s new items", name, n)

    try:
        n = transcribe_pending(cfg, store)
        if n:
            log.info("transcribed %d audio file(s)", n)
    except Exception:
        log.exception("transcription failed — continuing without it")

    digest, item_ids = build_digest(cfg, store, day)
    log.info("digest: %d chars from %d items", len(digest), len(item_ids))

    if args.dry_run:
        print(f"[dry-run] day={day} harvest={results} digest_chars={len(digest)} items={len(item_ids)}")
        if digest:
            (store.dir / f"digest-{day}-dryrun.md").write_text(digest, encoding="utf-8")
            print(f"[dry-run] digest written to {store.dir / f'digest-{day}-dryrun.md'}")
        return 0

    if not digest:
        notify(cfg, f"🌙 dailypost: no new work items for {day} — no draft today.")
        return 0

    (store.dir / f"digest-{day}.md").write_text(digest, encoding="utf-8")
    try:
        draft_ids, rejected = write_drafts(cfg, store, day, digest)
    except Exception as e:
        log.exception("draft failed")
        notify(cfg, f"⚠️ dailypost: draft generation failed for {day}: {e}")
        return 1

    store.mark_used(item_ids, day)

    if not draft_ids:
        notify(cfg, f"🌙 dailypost: harvested {sum(v for v in results.values() if v > 0)} items for {day}, "
                    "but nothing post-worthy today.")
        return 0

    deliver_drafts(cfg, store, draft_ids, rejected)
    prune_old_files(cfg, store)
    prune_old_images(cfg, store)

    # LinkedIn token expiry early warning
    try:
        from ..bot.linkedin_client import LinkedInClient
        days_left = LinkedInClient(cfg).days_until_expiry()
        if days_left is not None and days_left < 7:
            notify(cfg, f"⚠️ LinkedIn access token expires in {days_left} days — re-run: python -m server.bot.linkedin_auth")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
