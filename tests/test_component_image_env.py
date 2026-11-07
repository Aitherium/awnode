"""awnode's shipped ComfyUI manifests default to a PUBLIC image; an operator's own
build is selected by the env var each manifest names (AWND007: no fleet container
name ships in the package)."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from awnode import extensions as ext

COMPONENTS = Path(ext.__file__).resolve().parent / "components"

CASES = [
    ("comfyui.yaml", "AWNODE_COMFYUI_IMAGE"),
    ("comfyui-3d.yaml", "AWNODE_COMFYUI_3D_IMAGE"),
]


def _load(name: str) -> dict:
    return yaml.safe_load((COMPONENTS / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name,var", CASES)
def test_manifest_declares_its_override_variable(name, var):
    m = _load(name)
    assert m["image_env"] == var
    assert m["image"], "a docker manifest needs a default image"
    assert "aither" not in m["image"].lower(), m["image"]


@pytest.mark.parametrize("name,var", CASES)
def test_default_image_when_variable_unset(name, var, monkeypatch):
    monkeypatch.delenv(var, raising=False)
    m = _load(name)
    assert ext._from_addon(m).image == m["image"]


@pytest.mark.parametrize("name,var", CASES)
def test_variable_selects_the_operator_image(name, var, monkeypatch):
    monkeypatch.setenv(var, "registry.example/own-comfyui:1")
    assert ext._from_addon(_load(name)).image == "registry.example/own-comfyui:1"


def test_blank_variable_keeps_the_default(monkeypatch):
    monkeypatch.setenv("AWNODE_COMFYUI_IMAGE", "  ")
    m = _load("comfyui.yaml")
    assert ext._from_addon(m).image == m["image"]


def test_3d_default_listens_on_its_component_port():
    m = _load("comfyui-3d.yaml")
    assert str(m["default_port"]) in m["env_defaults"]["CLI_ARGS"]
