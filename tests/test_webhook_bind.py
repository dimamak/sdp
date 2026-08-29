"""Where the WAHA webhook receiver binds on the host.

The container always calls `host.docker.internal` (waha.compose.yml maps it with
extra_hosts). What that resolves to on the host is the part that differs: the
host's loopback under Docker Desktop, the docker0 bridge gateway under native
Linux Docker. Binding the wrong one means every inbound WhatsApp message is
dropped with no error anywhere — so this is worth pinning per platform.
"""
import subprocess

import pytest

import setup.wizard as wizard


class _Fake:
    """Stands in for subprocess.run over the two docker queries the helper makes."""

    def __init__(self, os_name="Docker Engine - Community", gateway="172.17.0.1"):
        self.os_name, self.gateway = os_name, gateway

    def __call__(self, cmd, **kw):
        text = " ".join(cmd)
        out = self.os_name if "info" in text else self.gateway
        return subprocess.CompletedProcess(cmd, 0, stdout=out + "\n", stderr="")


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_docker_desktop_platforms_bind_loopback(platform, monkeypatch):
    monkeypatch.setattr(wizard.sys, "platform", platform)
    monkeypatch.setattr(wizard.subprocess, "run",
                        lambda *a, **k: pytest.fail("no docker call needed on " + platform))
    host, why = wizard.webhook_bind_host()
    assert host == "127.0.0.1" and why


def test_docker_desktop_on_linux_also_binds_loopback(monkeypatch):
    # Docker Desktop for Linux runs the daemon in a VM: there is no docker0 on
    # the host, so the bridge gateway would be the wrong answer here
    monkeypatch.setattr(wizard.sys, "platform", "linux")
    monkeypatch.setattr(wizard.subprocess, "run", _Fake(os_name="Docker Desktop"))
    assert wizard.webhook_bind_host()[0] == "127.0.0.1"


def test_native_linux_docker_binds_the_bridge_gateway(monkeypatch):
    monkeypatch.setattr(wizard.sys, "platform", "linux")
    monkeypatch.setattr(wizard.subprocess, "run", _Fake(gateway="10.42.0.1"))
    host, why = wizard.webhook_bind_host()
    # asked docker, rather than assuming the conventional 172.17.0.1 — which is
    # simply absent when the daemon uses a different bridge subnet
    assert host == "10.42.0.1" and "10.42.0.1" in why


def test_an_unreadable_gateway_falls_back_loudly(monkeypatch):
    monkeypatch.setattr(wizard.sys, "platform", "linux")
    monkeypatch.setattr(wizard.subprocess, "run", _Fake(gateway=""))
    host, why = wizard.webhook_bind_host()
    assert host == "0.0.0.0"
    assert "firewall" in why.lower(), "a wide bind must come with a warning"


def test_garbage_from_docker_is_not_used_as_an_address(monkeypatch):
    monkeypatch.setattr(wizard.sys, "platform", "linux")
    monkeypatch.setattr(wizard.subprocess, "run", _Fake(gateway="<no value>"))
    assert wizard.webhook_bind_host()[0] == "0.0.0.0"
