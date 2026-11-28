"""`awnode mcp-config --format hermes` emits a Hermes Agent mcp_servers block that parses,
runs `awnode mcp` over stdio, and never prints the API key itself."""
from __future__ import annotations

import json

import yaml
from awnode.cli import mcp_client_config


def test_hermes_block_parses_and_runs_awnode_mcp():
    cfg = yaml.safe_load(mcp_client_config("hermes", "/usr/bin/awnode"))
    server = cfg["mcp_servers"]["awnode"]
    assert server == {"command": "/usr/bin/awnode", "args": ["mcp"]}


def test_hermes_block_references_the_key_never_embeds_it(monkeypatch):
    monkeypatch.setenv("AITHER_API_KEY", "real-key-value-xyz")
    text = mcp_client_config("hermes", "awnode", with_api_key=True)
    assert "real-key-value-xyz" not in text
    env = yaml.safe_load(text)["mcp_servers"]["awnode"]["env"]
    assert env == {"AITHER_API_KEY": "${AITHER_API_KEY}"}


def test_windows_path_survives_yaml_quoting():
    path = r"C:\Users\me\Scripts\awnode.exe"
    cfg = yaml.safe_load(mcp_client_config("hermes", path))
    assert cfg["mcp_servers"]["awnode"]["command"] == path


def test_mcp_json_shape_is_unchanged():
    cfg = json.loads(mcp_client_config("mcp-json", "awnode"))
    assert cfg["mcpServers"]["awnode"]["args"] == ["mcp"]
