# AwNode

Lightweight local gateway for AitherOS. Bridges your local apps to AI backends.

## Install

```bash
pip install awnode
# container image:
docker pull ghcr.io/aitherium/awnode:latest

# or full stack:
curl -fsSL https://launch.aitherium.com | bash
```

## Quick Start

```bash
# Start with auto-detection (finds Genesis, vLLM, Ollama, or cloud)
awnode start

# Run the published container
docker run --rm -p 8090:8090 ghcr.io/aitherium/awnode:latest

# Force specific backend
awnode start --vllm-url http://localhost:8120
awnode start --cloud
awnode start --local

# Check what's connected
awnode status

# Connect to Elysium cloud
awnode connect aither_sk_live_xxxxx

# Deploy via AitherComet
awnode deploy my-service --target docker --strategy rolling
```

## Backend Priority (auto mode)

1. **Genesis** (localhost:8001) — full AitherOS pipeline (context, memory, agents)
2. **vLLM** (localhost:8120) — direct GPU inference (OpenAI-compatible)
3. **Elysium** (cloud) — hosted AitherOS (requires API key)
4. **Ollama** (localhost:11434) — local CPU/GPU inference
5. **Standalone** — no LLM, tools-only mode

## API

```
GET  /health          — Health check
GET  /status          — Backend status (what's connected)
POST /chat            — Chat (proxied to best backend)
POST /deploy          — Deploy via AitherComet
POST /connect         — Register with Elysium
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `AITHER_URL` | `http://localhost:8001` | Genesis URL |
| `AITHER_VLLM_URL` | `http://localhost:8120` | vLLM URL |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama URL |
| `AITHER_API_KEY` | | API key for cloud/auth |
| `AITHERNODE_MODE` | `auto` | Force mode: auto/local/cloud/standalone |
| `AITHERNODE_PORT` | `8090` | Server port |

## Release Channels

- PyPI: `pip install awnode`
- Container: `ghcr.io/aitherium/awnode`
- Source tags: `awnode-v*`

---

## The aw family

Standalone tools that share one idea: **replace something you would otherwise have to _trust_ with something you can _check_.**

Each installs on its own, works offline, and needs no account.

| | instead of trusting | you check |
|---|---|---|
| [awnix](https://github.com/Aitherium/awnix) | that the box is what you left it as | an immutable image you built, with atomic rollback |
| **awnode** _(you are here)_ | a vendor's cloud with every prompt | a local gateway routing to backends you chose |
| [awgit](https://github.com/Aitherium/awgit) | that no one else is editing this file | a lease, refused at commit time if you do not hold it |
| [awgraph](https://github.com/Aitherium/awgraph) | that grep found everything | an AST + tree-sitter call graph an agent can traverse |
| [awseal](https://github.com/Aitherium/awseal) | that the artifact came from who you think | an Ed25519 seal — the key that verifies is not the key that forges |
| [awshare](https://github.com/Aitherium/awshare) | that the download is intact | content-addressed bundles, verified on fetch |
| [awrelay](https://github.com/Aitherium/awrelay) | a SaaS in the middle of your agents | findings, alerts and coordination over your own transport |
| [awm](https://github.com/Aitherium/awm) | that memory stayed in its lane | tenant:user:project scopes, so a write cannot cross a boundary |
| [awrecover](https://github.com/Aitherium/awrecover) | that the restore worked | a restore that fully lands or does not land at all |

[**awnix**](https://github.com/Aitherium/awnix) is the ground floor — a bootable, immutable Linux base for machines where software writes software.
