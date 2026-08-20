"""
AwNode License Enforcement
================================

Validates subscription status via ACTA (billing) and controls what
extensions, tools, agents, and capabilities are available locally.

Flow:
  1. On startup, validate API key against gateway.aitherium.com
  2. Cache the entitlement (plan, features, limits, expiry)
  3. Refresh every 30 minutes (or on demand)
  4. Gate extension installs, tool calls, and agent dispatch based on plan

If the subscription lapses:
  - Tools/agents/extensions that require a paid tier stop working
  - Free-tier tools (filesystem, git, search, Ollama) keep working
  - Clear error messages tell the user to renew

If offline:
  - Use cached entitlement (grace period: 7 days from last validation)
  - After grace period, fall back to free-tier capabilities only
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("awnode.license")

AITHER_HOME = Path(os.environ.get("AITHER_HOME", str(Path.home() / ".aither")))
LICENSE_CACHE = AITHER_HOME / "license.json"
GATEWAY_URL = os.environ.get("AITHER_CLOUD_URL", "https://gateway.aitherium.com")
API_KEY = os.environ.get("AITHER_API_KEY", "")

REFRESH_INTERVAL = 1800  # 30 minutes
GRACE_PERIOD = 7 * 86400  # 7 days offline grace


# ── Plan definitions (mirror ACTA PLANS) ──

PLAN_FEATURES: Dict[str, Dict[str, Any]] = {
    "free": {
        "tools": {"filesystem", "git_tools", "search", "commands", "context",
                  "memory", "knowledge", "notebooks", "documents", "mermaid",
                  "http_client", "ollama", "orchestrator", "embeddings"},
        "extensions": {"ollama"},
        "agents": set(),
        "max_agents": 1,
        "image_generation": False,
        "gpu_access": False,
        "multi_agent": False,
    },
    "developer": {
        "tools": {"filesystem", "git_tools", "search", "commands", "context",
                  "memory", "knowledge", "notebooks", "documents", "mermaid",
                  "http_client", "ollama", "orchestrator", "embeddings",
                  "generation", "extensions", "vision"},
        "extensions": {"ollama", "canvas", "comfyui"},
        "agents": {"iris"},
        "max_agents": 3,
        "image_generation": True,
        "gpu_access": True,
        "multi_agent": False,
    },
    "pro": {
        "tools": {"*"},  # All tools
        "extensions": {"ollama", "canvas", "comfyui", "comfyui-3d", "muse",
                       "sd-webui", "iris", "design"},
        "agents": {"iris", "demiurge", "atlas", "hydra", "viviane"},
        "max_agents": 15,
        "image_generation": True,
        "gpu_access": True,
        "multi_agent": True,
    },
    "team": {
        "tools": {"*"},
        "extensions": {"*"},  # All extensions
        "agents": {"*"},
        "max_agents": 30,
        "image_generation": True,
        "gpu_access": True,
        "multi_agent": True,
    },
    "business": {
        "tools": {"*"},
        "extensions": {"*"},
        "agents": {"*"},
        "max_agents": 50,
        "image_generation": True,
        "gpu_access": True,
        "multi_agent": True,
    },
    "enterprise": {
        "tools": {"*"},
        "extensions": {"*"},
        "agents": {"*"},
        "max_agents": 999,
        "image_generation": True,
        "gpu_access": True,
        "multi_agent": True,
    },
}

# EA tiers map to their closest equivalent
_EA_MAP = {
    "ea_pioneer": "pro",
    "ea_builder": "business",
    "ea_founder": "enterprise",
    "starter": "developer",
    "professional": "pro",
}


@dataclass
class Entitlement:
    """Cached subscription entitlement."""
    plan: str = "free"
    tier: str = "free"
    user_id: str = ""
    tenant_id: str = ""
    token_balance: int = 0
    features: List[str] = field(default_factory=list)
    validated_at: float = 0.0  # unix timestamp
    expires_at: float = 0.0
    active: bool = False

    @property
    def resolved_plan(self) -> str:
        return _EA_MAP.get(self.plan, self.plan)

    @property
    def plan_config(self) -> Dict[str, Any]:
        return PLAN_FEATURES.get(self.resolved_plan, PLAN_FEATURES["free"])

    @property
    def is_valid(self) -> bool:
        """Check if entitlement is still valid (not expired + within grace)."""
        if not self.active:
            return False
        now = time.time()
        if self.expires_at and now > self.expires_at:
            return False
        # Grace period: if we haven't validated in 7 days, fall back to free
        if now - self.validated_at > GRACE_PERIOD:
            return False
        return True

    @property
    def is_paid(self) -> bool:
        return self.resolved_plan != "free" and self.is_valid


class LicenseManager:
    """Manages subscription validation and entitlement checking."""

    def __init__(self):
        self.entitlement = Entitlement()
        self._load_cache()

    def _load_cache(self):
        """Load cached entitlement from disk."""
        if LICENSE_CACHE.exists():
            try:
                data = json.loads(LICENSE_CACHE.read_text(encoding="utf-8"))
                self.entitlement = Entitlement(**data)
            except Exception:
                pass

    def _save_cache(self):
        """Persist entitlement to disk."""
        AITHER_HOME.mkdir(parents=True, exist_ok=True)
        LICENSE_CACHE.write_text(
            json.dumps(asdict(self.entitlement), indent=2),
            encoding="utf-8",
        )

    async def validate(self, force: bool = False) -> Dict[str, Any]:
        """Validate subscription against ACTA via gateway.

        Returns entitlement status. Caches result for REFRESH_INTERVAL.
        """
        api_key = API_KEY or os.environ.get("AITHER_API_KEY", "")
        if not api_key:
            self.entitlement = Entitlement(
                plan="free", tier="free", active=True,
                validated_at=time.time(),
            )
            self._save_cache()
            return {"plan": "free", "status": "no_api_key"}

        # Check cache freshness
        now = time.time()
        if not force and (now - self.entitlement.validated_at) < REFRESH_INTERVAL:
            return {"plan": self.entitlement.plan, "status": "cached",
                    "valid": self.entitlement.is_valid}

        import httpx
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.get(
                    f"{GATEWAY_URL}/v1/billing/balance",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                if r.status_code == 200:
                    data = r.json()
                    self.entitlement = Entitlement(
                        plan=data.get("plan", "free"),
                        tier=data.get("tier", data.get("plan", "free")),
                        user_id=data.get("user_id", ""),
                        tenant_id=data.get("tenant_id", ""),
                        token_balance=data.get("tokens", 0),
                        features=data.get("features", []),
                        validated_at=now,
                        expires_at=data.get("expires_at", 0),
                        active=True,
                    )
                    self._save_cache()
                    logger.info("License validated: plan=%s, tokens=%d",
                                self.entitlement.plan, self.entitlement.token_balance)
                    return {"plan": self.entitlement.plan, "status": "validated",
                            "tokens": self.entitlement.token_balance}

                elif r.status_code == 401:
                    self.entitlement.active = False
                    self._save_cache()
                    return {"plan": "free", "status": "invalid_key"}

                elif r.status_code == 402:
                    # Key valid but no balance
                    self.entitlement.token_balance = 0
                    self.entitlement.validated_at = now
                    self._save_cache()
                    return {"plan": self.entitlement.plan, "status": "no_balance"}

                return {"plan": self.entitlement.plan, "status": f"http_{r.status_code}"}

        except Exception as e:
            # Offline — use cached entitlement with grace period
            if self.entitlement.is_valid:
                logger.info("Offline — using cached entitlement (plan=%s)",
                            self.entitlement.plan)
                return {"plan": self.entitlement.plan, "status": "offline_cached"}
            else:
                logger.warning("Offline and cache expired — falling back to free tier")
                return {"plan": "free", "status": "offline_expired"}

    # ── Entitlement checks ──

    def check_tool(self, tool_module: str) -> Optional[str]:
        """Check if a tool module is allowed by the current plan.

        Returns None if allowed, or an error message if denied.
        """
        config = self.entitlement.plan_config
        allowed = config.get("tools", set())
        if "*" in allowed or tool_module in allowed:
            return None
        plan = self.entitlement.resolved_plan
        return (
            f"Tool '{tool_module}' requires a paid subscription. "
            f"Current plan: {plan}. Upgrade at https://aitherium.com/pricing"
        )

    def check_extension(self, ext_id: str) -> Optional[str]:
        """Check if an extension is allowed by the current plan.

        Returns None if allowed, or an error message if denied.
        """
        config = self.entitlement.plan_config
        allowed = config.get("extensions", set())
        if "*" in allowed or ext_id in allowed:
            return None
        plan = self.entitlement.resolved_plan
        return (
            f"Extension '{ext_id}' requires a higher subscription tier. "
            f"Current plan: {plan}. Upgrade at https://aitherium.com/pricing"
        )

    def check_agent(self, agent_name: str) -> Optional[str]:
        """Check if an agent is allowed by the current plan.

        Returns None if allowed, or an error message if denied.
        """
        config = self.entitlement.plan_config
        allowed = config.get("agents", set())
        if "*" in allowed or agent_name in allowed:
            return None
        plan = self.entitlement.resolved_plan
        return (
            f"Agent '{agent_name}' requires a higher subscription tier. "
            f"Current plan: {plan}. Upgrade at https://aitherium.com/pricing"
        )

    def check_feature(self, feature: str) -> bool:
        """Check if a feature flag is enabled for the current plan."""
        config = self.entitlement.plan_config
        return config.get(feature, False)

    def get_limits(self) -> Dict[str, Any]:
        """Get current plan limits."""
        config = self.entitlement.plan_config
        return {
            "plan": self.entitlement.resolved_plan,
            "active": self.entitlement.is_valid,
            "paid": self.entitlement.is_paid,
            "max_agents": config.get("max_agents", 1),
            "image_generation": config.get("image_generation", False),
            "gpu_access": config.get("gpu_access", False),
            "multi_agent": config.get("multi_agent", False),
            "token_balance": self.entitlement.token_balance,
        }

    def to_dict(self) -> Dict[str, Any]:
        """Full entitlement state for status display."""
        return {
            "entitlement": asdict(self.entitlement),
            "resolved_plan": self.entitlement.resolved_plan,
            "is_valid": self.entitlement.is_valid,
            "is_paid": self.entitlement.is_paid,
            "limits": self.get_limits(),
        }


# Singleton
_license: Optional[LicenseManager] = None


def get_license_manager() -> LicenseManager:
    global _license
    if _license is None:
        _license = LicenseManager()
    return _license
