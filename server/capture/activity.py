"""Captures what happens OUTSIDE the coding agent: browsing, dashboards, docs,
design, admin. Two cheap complementary streams:

  1. activity log — the foreground window title every `sample_seconds`, written
     deduplicated as NDJSON. A few KB per day, and it tells the drafting model
     exactly where non-coding time went.
  2. screenshots — taken when the foreground APP changes (plus a periodic
     floor), downscaled JPEG, skipped when the screen is visually unchanged.

IDEs and terminals are logged by title but never screenshotted: that work is
already captured in full by the session transcripts.

PAUSE: create a file named PAUSED in the output folder; delete it to resume.

The NDJSON is rotated hourly, not daily. A file that is still being appended to
can't be ingested safely — its size changes between runs, so the size-derived
external_id changes and the same log is stored again and again. A closed hour
goes quiet, ages past the source's `min_age_seconds`, and drains exactly once.

Wayland is not supported: there is no compositor-independent way to read the
foreground window or grab the screen without an interactive portal prompt per
shot. The recorder refuses to start there rather than logging "?" all day.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from ..util import get_logger
from . import PAUSE_FILE

log = get_logger("capture.activity")

DEFAULT_SKIP_SHOT_APPS = ["Code", "devenv", "WindowsTerminal", "Terminal", "iTerm2",
                          "idea64", "pycharm64", "alacritty", "kitty", "gnome-terminal",
                          "cmd", "powershell", "pwsh", "conhost"]
DEDUP_DISTANCE = 3


# ---------------------------------------------------------------------------
# per-OS backends: foreground() -> (app, title)  and  idle_seconds() -> float
# ---------------------------------------------------------------------------

class Backend:
    name = "none"

    def foreground(self) -> tuple[str, str]:
        raise NotImplementedError

    def idle_seconds(self) -> float:
        raise NotImplementedError


class WindowsBackend(Backend):
    name = "windows"

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.u32 = ctypes.windll.user32
        self.k32 = ctypes.windll.kernel32
        self.ctypes, self.wintypes = ctypes, wintypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]
        self._lii = LASTINPUTINFO
        self._psapi_cache: dict[int, str] = {}

    def _process_name(self, pid: int) -> str:
        if pid in self._psapi_cache:
            return self._psapi_cache[pid]
        ctypes = self.ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = self.k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        name = "?"
        if h:
            try:
                buf = ctypes.create_unicode_buffer(512)
                size = self.wintypes.DWORD(512)
                if self.k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    name = Path(buf.value).stem
            finally:
                self.k32.CloseHandle(h)
        self._psapi_cache[pid] = name
        return name

    def foreground(self) -> tuple[str, str]:
        ctypes = self.ctypes
        h = self.u32.GetForegroundWindow()
        if not h:
            return "", ""
        buf = ctypes.create_unicode_buffer(512)
        self.u32.GetWindowTextW(h, buf, 512)
        pid = self.wintypes.DWORD()
        self.u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        return self._process_name(pid.value), buf.value

    def idle_seconds(self) -> float:
        lii = self._lii()
        lii.cbSize = self.ctypes.sizeof(lii)
        if not self.u32.GetLastInputInfo(self.ctypes.byref(lii)):
            return 0.0
        return max(0.0, (self.k32.GetTickCount() - lii.dwTime) / 1000.0)


class MacBackend(Backend):
    name = "macos"

    def __init__(self):
        import Quartz
        from AppKit import NSWorkspace
        self._workspace = NSWorkspace
        self._quartz = Quartz
        self.titles_available = self._probe_titles()
        if not self.titles_available:
            log.warning("macOS Accessibility permission not granted — logging app "
                        "names without window titles. Grant it in System Settings > "
                        "Privacy & Security > Accessibility to capture titles.")

    def _probe_titles(self) -> bool:
        try:
            infos = self._quartz.CGWindowListCopyWindowInfo(
                self._quartz.kCGWindowListOptionOnScreenOnly
                | self._quartz.kCGWindowListExcludeDesktopElements,
                self._quartz.kCGNullWindowID)
        except Exception:
            return False
        return any(w.get("kCGWindowName") for w in (infos or []))

    def foreground(self) -> tuple[str, str]:
        active = self._workspace.sharedWorkspace().frontmostApplication()
        if active is None:
            return "", ""
        app = str(active.localizedName() or "?")
        if not self.titles_available:
            return app, app
        pid = int(active.processIdentifier())
        infos = self._quartz.CGWindowListCopyWindowInfo(
            self._quartz.kCGWindowListOptionOnScreenOnly
            | self._quartz.kCGWindowListExcludeDesktopElements,
            self._quartz.kCGNullWindowID) or []
        for w in infos:
            if int(w.get("kCGWindowOwnerPID", -1)) == pid and w.get("kCGWindowName"):
                return app, str(w["kCGWindowName"])
        return app, app

    def idle_seconds(self) -> float:
        q = self._quartz
        return float(q.CGEventSourceSecondsSinceLastEventType(
            q.kCGEventSourceStateHIDSystemState, q.kCGAnyInputEventType))


class X11Backend(Backend):
    name = "x11"

    def __init__(self):
        from Xlib import display
        self._display = display.Display()
        self._root = self._display.screen().root
        self._net_active = self._display.intern_atom("_NET_ACTIVE_WINDOW")
        self._net_name = self._display.intern_atom("_NET_WM_NAME")
        self._utf8 = self._display.intern_atom("UTF8_STRING")
        self._net_pid = self._display.intern_atom("_NET_WM_PID")
        self._screensaver = self._probe_screensaver()

    def _probe_screensaver(self):
        try:
            from Xlib.ext import screensaver  # noqa: F401
            return self._display.screensaver_query_info
        except Exception:
            return None

    def _active_window(self):
        prop = self._root.get_full_property(self._net_active, 0)
        if not prop or not prop.value:
            return None
        return self._display.create_resource_object("window", prop.value[0])

    def foreground(self) -> tuple[str, str]:
        try:
            win = self._active_window()
            if win is None:
                return "", ""
            title = win.get_full_property(self._net_name, self._utf8)
            title = title.value.decode("utf-8", "replace") if title else (win.get_wm_name() or "")
            app = "?"
            pid = win.get_full_property(self._net_pid, 0)
            if pid and pid.value:
                try:
                    app = Path(Path(f"/proc/{pid.value[0]}/comm").read_text().strip()).stem
                except OSError:
                    pass
            if app == "?":
                cls = win.get_wm_class()
                app = cls[1] if cls else "?"
            return app, title
        except Exception:
            return "", ""

    def idle_seconds(self) -> float:
        if self._screensaver is None:
            return 0.0
        try:
            return float(self._screensaver(self._root).idle) / 1000.0
        except Exception:
            return 0.0


def make_backend(platform: str | None = None) -> Backend:
    """Raises RuntimeError with an actionable message rather than degrading to
    a log full of "?" — an activity log that records nothing readable is worse
    than no activity log, because it looks like it is working."""
    platform = platform or sys.platform
    if platform == "win32":
        return WindowsBackend()
    if platform == "darwin":
        try:
            return MacBackend()
        except ImportError as e:
            raise RuntimeError(
                "pyobjc is required on macOS — install requirements-capture.txt") from e
    if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" or os.environ.get(
            "WAYLAND_DISPLAY"):
        raise RuntimeError(
            "Wayland cannot report the foreground window or grab the screen without "
            "an interactive portal prompt per screenshot, so activity capture is not "
            "supported there. Log in with an X11/Xorg session, or leave "
            "activity.enabled: false.")
    if not os.environ.get("DISPLAY"):
        raise RuntimeError("no DISPLAY — activity capture needs a graphical session")
    try:
        return X11Backend()
    except ImportError as e:
        raise RuntimeError(
            "python-xlib is required on Linux — install requirements-capture.txt") from e


# ---------------------------------------------------------------------------
# screenshots
# ---------------------------------------------------------------------------

def ahash(image) -> str:
    """64-bit average hash: unchanged by a ticking clock or a blinking cursor,
    different as soon as the actual content is."""
    small = image.convert("L").resize((8, 8))
    pixels = list(small.getdata())
    avg = sum(pixels) / len(pixels)
    return "".join("1" if p >= avg else "0" for p in pixels)


def hash_distance(a: str, b: str) -> int:
    if not a or not b or len(a) != len(b):
        return 999
    return sum(x != y for x, y in zip(a, b, strict=True))


def save_screenshot(path: Path, prev_hash: str, max_width: int, quality: int) -> str | None:
    """Returns the hash of what was written, or None if the screen was unchanged
    (in which case nothing is written)."""
    import mss
    from PIL import Image

    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[0])       # monitor 0 = the whole virtual desktop
    img = Image.frombytes("RGB", shot.size, shot.rgb)
    h = ahash(img)
    if hash_distance(h, prev_hash) <= DEDUP_DISTANCE:
        return None
    if img.width > max_width:
        img = img.resize((max_width, round(img.height * max_width / img.width)))
    img.save(path, "JPEG", quality=quality)
    return h


# ---------------------------------------------------------------------------
# the sampling loop
# ---------------------------------------------------------------------------

def log_path(out_dir: Path, now: datetime) -> Path:
    """Hourly, not daily — see the module docstring on why a growing file can't
    be drained safely."""
    return out_dir / f"activity-{now.strftime('%Y%m%d-%H')}.ndjson"


def status(cfg) -> str:
    if not cfg.get("activity.enabled", False):
        return "activity capture: off"
    out_dir = Path(str(cfg.get("activity.out_dir", "~/.dailypost/activity"))).expanduser()
    if (out_dir / PAUSE_FILE).exists():
        return f"activity capture: PAUSED (delete {out_dir / PAUSE_FILE} to resume)"
    if not out_dir.exists():
        return "activity capture: on, output dir missing"
    current = log_path(out_dir, datetime.now())
    lines = 0
    if current.exists():
        lines = sum(1 for _ in current.open(encoding="utf-8", errors="replace"))
    shots = len(list(out_dir.glob("*.jpg")))
    return (
        f"activity capture: on, {lines} window change(s) this hour, "
        f"{shots} screenshot(s) waiting")


def sample_loop(cfg, stop_event: threading.Event | None = None,
                out_dir: Path | None = None) -> None:
    stop_event = stop_event or threading.Event()
    out_dir = out_dir or Path(str(cfg.get("activity.out_dir",
                                          "~/.dailypost/activity"))).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        backend = make_backend()
    except RuntimeError as e:
        log.error("activity capture not started: %s", e)
        return
    except Exception:
        log.exception("activity capture not started: backend unavailable")
        return

    interval = int(cfg.get("activity.sample_seconds", 30))
    idle_cutoff = float(cfg.get("activity.idle_seconds", 180))
    shot_gap = float(cfg.get("activity.screenshot_seconds", 300))
    max_width = int(cfg.get("activity.screenshot_max_width", 1280))
    quality = int(cfg.get("activity.screenshot_quality", 60))
    skip_apps = {a.lower() for a in
                 (cfg.get("activity.skip_shot_apps", DEFAULT_SKIP_SHOT_APPS) or [])}
    pause_file = out_dir / PAUSE_FILE
    log.info("activity capture started (%s backend, %ds sampling) -> %s; pause with %s",
             backend.name, interval, out_dir, pause_file)

    last_title, last_app, last_hash = "", "", ""
    last_shot = 0.0
    shots_ok = True
    while not stop_event.is_set():
        stop_event.wait(interval)
        if stop_event.is_set() or pause_file.exists():
            continue
        try:
            # away from the machine: the screen isn't changing and nothing is
            # being done, so neither a log line nor a screenshot carries anything
            if backend.idle_seconds() >= idle_cutoff:
                continue
            app, title = backend.foreground()
            if not title:
                continue
            now = datetime.now()
            if title != last_title:
                rec = {"ts": now.astimezone(timezone.utc).isoformat(),
                       "app": app, "title": title}
                with log_path(out_dir, now).open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                last_title = title
            want_shot = (app != last_app or time.time() - last_shot >= shot_gap)
            if want_shot and shots_ok and app.lower() not in skip_apps:
                name = f"screen-{now.strftime('%Y%m%d-%H%M%S')}-{app}.jpg"
                try:
                    h = save_screenshot(out_dir / name, last_hash, max_width, quality)
                except ImportError:
                    log.error("screenshots need Pillow + mss (requirements-capture.txt) "
                              "— continuing with the window log only")
                    shots_ok = False
                    h = None
                except Exception as e:
                    log.warning("screenshot failed: %s", e)
                    h = None
                if h:
                    last_hash = h
                last_shot = time.time()
            last_app = app
        except Exception:
            log.exception("activity sample failed — continuing")
    log.info("activity capture stopped")


def start(cfg) -> threading.Event:
    stop = threading.Event()
    threading.Thread(target=sample_loop, args=(cfg, stop),
                     daemon=True, name="activity-recorder").start()
    return stop


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config")
    ap.add_argument("--out-dir", help="override activity.out_dir (server-mode laptops)")
    args = ap.parse_args(argv)
    from ..config import Config
    cfg = Config.load(args.config)
    try:
        sample_loop(cfg, out_dir=Path(args.out_dir).expanduser() if args.out_dir else None)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
