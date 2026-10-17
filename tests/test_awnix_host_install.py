"""awnode on an awnix/podman host: service scope and container-engine choice.

No host is touched: PATH lookups, euid and the WSL marker are patched.
"""

from pathlib import Path

import pytest

from awnode import extensions, service_install


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv(service_install.SCOPE_ENV, raising=False)
    monkeypatch.delenv(extensions.ENGINE_ENV, raising=False)


def _host(monkeypatch, *, root: bool, wsl: bool, tmp_path: Path):
    monkeypatch.setattr(service_install, "_is_root", lambda: root)
    marker = tmp_path / "WSLInterop"
    if wsl:
        marker.write_text("enabled")
    monkeypatch.setattr(service_install, "_WSL_MARKER", marker)


def test_plain_user_gets_a_user_unit(monkeypatch, tmp_path):
    _host(monkeypatch, root=False, wsl=False, tmp_path=tmp_path)
    assert service_install._systemd_scope() == "user"
    argv = service_install._systemctl("user", "start", "awnode.service")
    assert argv[:2] == ["systemctl", "--user"]


def test_root_gets_a_system_unit(monkeypatch, tmp_path):
    _host(monkeypatch, root=True, wsl=False, tmp_path=tmp_path)
    assert service_install._systemd_scope() == "system"
    assert service_install._systemd_unit_path() == Path("/etc/systemd/system/awnode.service")
    assert service_install._systemctl("system", "daemon-reload") == ["systemctl", "daemon-reload"]


def test_wsl_non_root_gets_a_system_unit_through_sudo(monkeypatch, tmp_path):
    # No user systemd instance exists under WSL: a user unit would never start.
    _host(monkeypatch, root=False, wsl=True, tmp_path=tmp_path)
    assert service_install._systemd_scope() == "system"
    assert service_install._systemctl("system", "start", "x")[:3] == ["sudo", "-n", "systemctl"]


def test_scope_env_pins(monkeypatch, tmp_path):
    _host(monkeypatch, root=True, wsl=True, tmp_path=tmp_path)
    monkeypatch.setenv(service_install.SCOPE_ENV, "user")
    assert service_install._systemd_scope() == "user"


def test_system_unit_runs_as_the_invoking_user():
    text = service_install._render_unit("/usr/bin/awnode start", "system", "david")
    assert "User=david\n" in text
    assert "WantedBy=multi-user.target" in text
    user_text = service_install._render_unit("/usr/bin/awnode start", "user", "david")
    assert "User=" not in user_text
    assert "WantedBy=default.target" in user_text


def test_engine_prefers_podman(monkeypatch):
    found = {"podman": "/usr/bin/podman", "docker": "/usr/bin/docker"}
    monkeypatch.setattr("shutil.which", lambda c: found.get(c))
    assert extensions.container_engine() == "podman"
    assert extensions.gpu_run_flags("podman") == ["--device", "nvidia.com/gpu=all"]
    assert extensions.gpu_run_flags("docker") == ["--gpus", "all"]


def test_engine_falls_back_to_docker_then_none(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda c: "/usr/bin/docker" if c == "docker" else None)
    assert extensions.container_engine() == "docker"
    monkeypatch.setattr("shutil.which", lambda c: None)
    assert extensions.container_engine() is None


def test_engine_env_pin_must_exist(monkeypatch):
    monkeypatch.setenv(extensions.ENGINE_ENV, "docker")
    monkeypatch.setattr("shutil.which", lambda c: "/x/podman" if c == "podman" else None)
    assert extensions.container_engine() is None


def test_existing_user_unit_keeps_user_scope_under_wsl(monkeypatch, tmp_path):
    # A user unit installed before the system-scope rule must stay manageable:
    # status/uninstall looking in /etc/systemd/system would orphan it.
    _host(monkeypatch, root=False, wsl=True, tmp_path=tmp_path)
    home = tmp_path / "home"
    unit = home / ".config" / "systemd" / "user" / service_install.SYSTEMD_UNIT
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    assert service_install._systemd_scope() == "system"
    unit.parent.mkdir(parents=True)
    unit.write_text("[Unit]\n")
    assert service_install._systemd_scope() == "user"
