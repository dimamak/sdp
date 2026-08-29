"""Keep the laptop-mode bot process running across logins and reboots.

In laptop mode the bot process is not a convenience. The nightly scheduler lives
in it (`server/bot/scheduler.py`), and so do both recorders — so a laptop install
where nobody remembered to start the process looks exactly like a working one
until the digests never arrive.

Each OS gets its own native mechanism, all of them per-user: nothing here needs
root, and nothing here writes outside the person's home directory.

The unit/plist/script *text* is built by pure functions that take every path as
an argument, so they can be asserted on any OS; only `install()` and `status()`
touch the machine.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

SERVICE_NAME = "dailypost-bot"       # systemd --user unit, Windows task name
MAC_LABEL = "ai.dailypost.bot"       # launchd label


def systemd_unit(python: Path, repo: Path, config: Path) -> str:
    return f"""[Unit]
Description=Social Daily Poster bot (laptop mode)
After=network-online.target

[Service]
Type=simple
WorkingDirectory={repo}
Environment=DAILYPOST_CONFIG={config}
ExecStart={python} -m server.bot.main
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
"""


def launchd_plist(python: Path, repo: Path, config: Path, log: Path) -> str:
    e = lambda p: escape(str(p))  # noqa: E731 — paths under ~ routinely contain &
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{MAC_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{e(python)}</string>
    <string>-m</string>
    <string>server.bot.main</string>
  </array>
  <key>WorkingDirectory</key><string>{e(repo)}</string>
  <key>EnvironmentVariables</key>
  <dict><key>DAILYPOST_CONFIG</key><string>{e(config)}</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{e(log)}</string>
  <key>StandardErrorPath</key><string>{e(log)}</string>
</dict>
</plist>
"""


def vbs_launcher(python: Path, repo: Path, config: Path) -> str:
    """Launch through wscript with window style 0: the reliable way to get a
    genuinely hidden but still INTERACTIVE process. Interactive matters — screen
    capture needs a real desktop — and python.exe run directly from Task
    Scheduler otherwise keeps a console window.

    Quotes are assembled with Chr(34) rather than escaped inline: paths contain
    spaces, and nested quote-doubling is exactly where this breaks silently.
    """
    return "\n".join([
        "Dim q, cmd, sh",
        "q = Chr(34)",
        f'cmd = q & "{python}" & q & " -m server.bot.main"',
        'Set sh = CreateObject("WScript.Shell")',
        f'sh.Environment("PROCESS").Item("DAILYPOST_CONFIG") = "{config}"',
        f'sh.CurrentDirectory = "{repo}"',   # `python -m` resolves the package from cwd
        "sh.Run cmd, 0, False",
    ]) + "\n"


def win_task_xml(user: str, exe: Path, args: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId></LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <StartWhenAvailable>true</StartWhenAvailable>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(exe))}</Command>
      <Arguments>{escape(args)}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


# ---------------------------------------------------------------------------
# installation
# ---------------------------------------------------------------------------

def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def _install_systemd(python: Path, repo: Path, config: Path) -> list[str]:
    unit_dir = Path("~/.config/systemd/user").expanduser()
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / f"{SERVICE_NAME}.service"
    unit.write_text(systemd_unit(python, repo, config), encoding="utf-8")
    _run(["systemctl", "--user", "daemon-reload"])
    out = _run(["systemctl", "--user", "enable", "--now", SERVICE_NAME])
    if out.returncode != 0:
        raise RuntimeError(f"systemctl --user enable failed: {out.stderr.strip()}")
    notes = [f"installed {unit}", f"logs: journalctl --user -u {SERVICE_NAME} -f"]
    linger = _run(["loginctl", "show-user", os.environ.get("USER", ""),
                   "--property=Linger"]).stdout.strip()
    if not linger.endswith("=yes"):
        # Without linger a --user unit is killed the moment the last session
        # closes, which is exactly what happens on a headless or remote box.
        notes.append(f"to survive logout, run once: sudo loginctl enable-linger "
                     f"{os.environ.get('USER', '$USER')}")
    return notes


def _install_launchd(python: Path, repo: Path, config: Path) -> list[str]:
    agents = Path("~/Library/LaunchAgents").expanduser()
    agents.mkdir(parents=True, exist_ok=True)
    plist = agents / f"{MAC_LABEL}.plist"
    log = Path("~/Library/Logs/dailypost-bot.log").expanduser()
    log.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(launchd_plist(python, repo, config, log), encoding="utf-8")
    target = f"gui/{os.getuid()}"
    _run(["launchctl", "bootout", f"{target}/{MAC_LABEL}"])  # replacing, may not exist
    out = _run(["launchctl", "bootstrap", target, str(plist)])
    if out.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {out.stderr.strip()}")
    _run(["launchctl", "enable", f"{target}/{MAC_LABEL}"])
    return [f"installed {plist}", f"logs: {log}"]


def _install_schtasks(python: Path, repo: Path, config: Path) -> list[str]:
    vbs = repo / f"{SERVICE_NAME}.vbs"
    vbs.write_text(vbs_launcher(python, repo, config), encoding="ascii")
    exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "wscript.exe"
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}"
    xml = win_task_xml(user, exe, f'"{vbs}"')
    tmp = Path(os.environ.get("TEMP", ".")) / f"{SERVICE_NAME}.xml"
    tmp.write_text(xml, encoding="utf-16")  # schtasks /XML requires UTF-16
    try:
        out = _run(["schtasks", "/Create", "/F", "/TN", SERVICE_NAME, "/XML", str(tmp)])
    finally:
        tmp.unlink(missing_ok=True)
    if out.returncode != 0:
        raise RuntimeError(f"schtasks /Create failed: {(out.stderr or out.stdout).strip()}")
    _run(["schtasks", "/Run", "/TN", SERVICE_NAME])
    return [f"registered logon task: {SERVICE_NAME}", f"launcher: {vbs}"]


def install(python: Path, repo: Path, config: Path, platform: str | None = None) -> list[str]:
    """Register the bot with the OS. Returns human-readable notes; raises on failure."""
    platform = platform or sys.platform
    if platform == "win32":
        return _install_schtasks(python, repo, config)
    if platform == "darwin":
        return _install_launchd(python, repo, config)
    return _install_systemd(python, repo, config)


def state(platform: str | None = None) -> tuple[bool, str]:
    """(will the OS start the bot?, one line saying so). Never raises."""
    platform = platform or sys.platform
    try:
        if platform == "win32":
            out = _run(["schtasks", "/Query", "/TN", SERVICE_NAME])
            return ((True, "logon task registered") if out.returncode == 0
                    else (False, "no logon task"))
        if platform == "darwin":
            out = _run(["launchctl", "print", f"gui/{os.getuid()}/{MAC_LABEL}"])
            return ((True, "launch agent loaded") if out.returncode == 0
                    else (False, "no launch agent"))
        enabled = _run(["systemctl", "--user", "is-enabled", SERVICE_NAME]).stdout.strip()
        active = _run(["systemctl", "--user", "is-active", SERVICE_NAME]).stdout.strip()
        if enabled != "enabled":
            return False, f"systemd --user unit {enabled or 'not installed'}"
        return True, f"systemd --user unit enabled, {active or 'unknown'}"
    except FileNotFoundError:
        return False, "no autostart mechanism available on this machine"


def status(platform: str | None = None) -> str:
    return state(platform)[1]
