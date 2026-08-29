"""Autostart unit text, asserted on every OS in the matrix.

The install functions shell out to schtasks/launchctl/systemctl and so can only
run on their own OS, but the files they write are built by pure functions — and
those files are where this historically goes wrong: a path with a space, an
unescaped ampersand in a username, a working directory that makes `python -m`
fail to find the package.
"""
import sys

import pytest

from setup import autostart

PY = "/home/a b/.venv/bin/python"
REPO = "/home/a b/dailypost"
CFG = "/home/a b/dailypost/config.yaml"


# ---------------------------------------------------------------------------
# systemd --user
# ---------------------------------------------------------------------------

def test_systemd_unit_restarts_and_starts_without_a_login_session():
    unit = autostart.systemd_unit(PY, REPO, CFG)
    assert f"ExecStart={PY} -m server.bot.main" in unit
    assert f"WorkingDirectory={REPO}" in unit
    assert f"Environment=DAILYPOST_CONFIG={CFG}" in unit
    assert "Restart=always" in unit
    # default.target, not multi-user: a --user unit has no multi-user.target
    assert "WantedBy=default.target" in unit


# ---------------------------------------------------------------------------
# launchd
# ---------------------------------------------------------------------------

def test_launchd_plist_is_valid_xml_and_keeps_the_bot_alive():
    import plistlib

    plist = autostart.launchd_plist(PY, REPO, CFG, "/tmp/bot.log")
    parsed = plistlib.loads(plist.encode())
    assert parsed["Label"] == autostart.MAC_LABEL
    assert parsed["ProgramArguments"] == [PY, "-m", "server.bot.main"]
    assert parsed["WorkingDirectory"] == REPO
    assert parsed["EnvironmentVariables"]["DAILYPOST_CONFIG"] == CFG
    assert parsed["RunAtLoad"] is True and parsed["KeepAlive"] is True


def test_launchd_plist_escapes_paths_that_would_break_the_xml():
    import plistlib

    weird = "/Users/a&b/Tom's <repo>"
    plist = autostart.launchd_plist(PY, weird, CFG, "/tmp/bot.log")
    assert plistlib.loads(plist.encode())["WorkingDirectory"] == weird


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

def test_vbs_launcher_quotes_the_python_path_and_sets_the_working_dir():
    vbs = autostart.vbs_launcher(PY, REPO, CFG)
    # the path has a space in it; the quoting must come from Chr(34), not from
    # literal quotes that PowerShell/VBScript nesting would eat
    assert 'q = Chr(34)' in vbs
    assert f'cmd = q & "{PY}" & q & " -m server.bot.main"' in vbs
    assert f'sh.CurrentDirectory = "{REPO}"' in vbs
    assert "sh.Run cmd, 0, False" in vbs   # 0 = hidden, False = don't wait


def test_win_task_xml_is_valid_and_triggers_at_logon():
    from xml.etree import ElementTree

    xml = autostart.win_task_xml("CORP\\alice", "C:\\Windows\\wscript.exe", '"C:\\a b.vbs"')
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    root = ElementTree.fromstring(xml)
    assert root.find(".//t:LogonTrigger/t:UserId", ns).text == "CORP\\alice"
    # interactive, not a service account: screen capture needs a real desktop
    assert root.find(".//t:LogonType", ns).text == "InteractiveToken"
    assert root.find(".//t:RunLevel", ns).text == "LeastPrivilege"
    # PT0S = no time limit; the default 72h would silently kill the bot
    assert root.find(".//t:ExecutionTimeLimit", ns).text == "PT0S"


def test_win_task_xml_escapes_an_ampersand_in_the_user_name():
    from xml.etree import ElementTree

    xml = autostart.win_task_xml("R&D\\bob", "C:\\wscript.exe", '"x.vbs"')
    ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert ElementTree.fromstring(xml).find(".//t:LogonTrigger/t:UserId", ns).text == "R&D\\bob"


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("platform,fn", [
    ("win32", "_install_schtasks"),
    ("darwin", "_install_launchd"),
    ("linux", "_install_systemd"),
    ("freebsd13", "_install_systemd"),   # anything else gets the systemd path
])
def test_install_dispatches_on_platform(platform, fn, monkeypatch):
    called = []
    monkeypatch.setattr(autostart, fn, lambda *a: called.append(fn) or ["done"])
    assert autostart.install(PY, REPO, CFG, platform) == ["done"]
    assert called == [fn]


def test_state_reports_a_missing_mechanism_rather_than_raising(monkeypatch):
    monkeypatch.setattr(autostart, "_run",
                        lambda cmd: (_ for _ in ()).throw(FileNotFoundError(cmd[0])))
    installed, msg = autostart.state(sys.platform)
    assert installed is False and msg
