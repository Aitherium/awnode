"""
Tool Scope Definitions for Standalone awnode
==================================================

Hardcoded from ``_tool_scopes.yaml`` since we cannot ship the YAML file
in the pip package. These sets determine which tool modules are available
in each runtime mode.

Three scopes:
    SHELL_LOCAL     Works fully offline (~30 tool modules)
    SHELL_CLOUD     Needs API key / Elysium gateway (~65 modules total)
    PLATFORM_ONLY   Needs full AitherOS deployment (~69 modules)

Tool modules not listed in any set default to PLATFORM_ONLY.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# shell_local — Available in standalone mode (no platform, no API key)
# ---------------------------------------------------------------------------

SHELL_LOCAL: set[str] = {
    # Core chat & inference
    "orchestrator",
    "ollama",
    "reasoning",
    "reasoning_config",

    # Code intelligence
    "codegraph",
    "codegraph_registry",
    "codebase_intel",
    "repowise",
    "exploration",

    # File & git operations
    "filesystem",
    "git_tools",
    "search",

    # Memory & context
    "memory",
    "context",
    "knowledge",
    "graph",
    "embeddings",

    # Agent operations
    "agent_delegate",
    "agent_skills",
    "forge",
    "fleet",
    "intent",
    "intent_planner",
    "persona",
    "soul",

    # Developer tools
    "commands",
    "http_client",
    "mermaid",
    "notebooks",
    "documents",
}

# ---------------------------------------------------------------------------
# shell_cloud — Available via Elysium gateway (includes shell_local)
# ---------------------------------------------------------------------------

SHELL_CLOUD: set[str] = SHELL_LOCAL | {
    # Extended agent capabilities
    "swarm",
    "cognitive",
    "context_synthesis",
    "condenser",
    "learning",

    # Cloud inference
    "llmfit",
    "model_fleet",
    "model_stacks",

    # Content & media
    "generation",
    "multimodal",
    "vision",
    "audio",
    "voice",

    # Business tools
    "billing",
    "account",
    "marketplace",
    "agent_marketplace",

    # Collaboration
    "relay",
    "email",
    "social",

    # Cloud deployment
    "cloud_deploy",
    "cloud_training",
    "vastai",
    "vastai_host",
    "gpu_deployments",
    "dgx",

    # Knowledge tools
    "wiki",
    "raganything",
    "blog",
    "blog_video",

    # External integrations
    "external",
    "gateway",
}

# ---------------------------------------------------------------------------
# platform_only — Requires full AitherOS deployment
# ---------------------------------------------------------------------------

PLATFORM_ONLY: set[str] = {
    # Infrastructure
    "infrastructure",
    "services",
    "mesh",
    "k3s",
    "deploy",
    "framework_deploy",
    "managed_services",
    "network_inspector",

    # Security & compliance
    "rbac",
    "permissions",
    "secrets",
    "security_ops",
    "compliance",
    "themis",

    # System internals
    "admin",
    "diagnostics",
    "chaos",
    "strata",
    "llm_traces",
    "tool_traces",
    "system_fingerprint",
    "kernels",

    # Training pipeline
    "training",
    "session_harvester",
    "evolution",
    "tokenizer",

    # Automation
    "automation",
    "dynamic_workflows",
    "workflows",
    "pipelines",
    "flow",

    # Creative pipeline
    "design",
    "design_system",
    "character_pipeline",
    "human_figure",
    "video",
    "video_produce",
    "video_production",
    "remotion",
    "roboflow",
    "roboflow_3d",
    "omniparser",
    "media_extract",
    "media_hosting",

    # Platform features
    "workspace_profile",
    "tenant_onboarding",
    "onboarding",
    "companion",
    "desktop_context",
    "browser_context",
    "eventgraph",
    "storage",
    "releases",
    "oss_tracking",
    "specs",
    "roadmap",
    "review",
    "saga",
    "sdd",
    "strategy",
    "business",
    "employment",
    "appforge",
    "content_pipeline",
    "content_transform",
    "html_presentation",
    "presentation",
    "neocities",
    "repo_onboard",
    "world_model",
    "acc",
    "sttp",
}
