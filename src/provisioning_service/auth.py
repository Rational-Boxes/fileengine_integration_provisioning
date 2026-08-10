# Copyright (C) 2026 James Hickman
# AGPL-3.0-or-later — see package header.
"""Identity + authorization for a provisioning request (SPEC §3).

Pure authorization logic (claim extraction, capability gate, source-IP `aip` gate)
lives here and is unit-testable; the thin FastAPI dependencies in ``deps.py`` compose
it with request state."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from . import netutil


class AuthError(Exception):
    """Authorization failure. ``status`` is the HTTP code to return."""

    def __init__(self, message: str, status: int = 403, code: str = "forbidden"):
        self.message = message
        self.status = status
        self.code = code
        super().__init__(message)


@dataclass
class IntegrationContext:
    """The integration-service identity + its provisioning scope (§3.1)."""

    integration_id: str
    tenant: str
    amr: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    prov_tenants: list[str] = field(default_factory=lambda: ["*"])
    prov_roots: list[str] = field(default_factory=list)
    prov_principals: list[str] = field(default_factory=list)
    prov_actions: Any = "*"          # list[str] | "*"
    prov_resources: Any = "*"        # list[str] | "*"
    prov_namespace: str = ""
    aip: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    # -- scope predicates --
    def is_integration(self) -> bool:
        return "integration" in self.amr and "provisioning" in self.capabilities

    def tenant_allowed(self, tenant: str) -> bool:
        pats = self.prov_tenants or ["*"]
        if "*" in pats:
            return True
        # simple exact / suffix-glob match (prefix* patterns)
        for p in pats:
            if p == tenant or (p.endswith("*") and tenant.startswith(p[:-1])):
                return True
        return False

    def action_allowed(self, machine_name: str) -> bool:
        if self.prov_actions in ("*", None) or self.prov_actions == ["*"]:
            return True
        return machine_name in self.prov_actions

    def resource_allowed(self, rtype: str) -> bool:
        if self.prov_resources in ("*", None) or self.prov_resources == ["*"]:
            return True
        return rtype in self.prov_resources


def context_from_claims(claims: dict[str, Any]) -> IntegrationContext:
    """Build an :class:`IntegrationContext` from verified JWT claims."""
    def _list(v, default=None):
        if v is None:
            return list(default or [])
        return list(v) if isinstance(v, (list, tuple)) else [v]

    return IntegrationContext(
        integration_id=str(claims.get("sub") or claims.get("azp") or ""),
        tenant=str(claims.get("tenant") or ""),
        amr=_list(claims.get("amr")),
        capabilities=_list(claims.get("capabilities") or claims.get("caps")),
        prov_tenants=_list(claims.get("prov_tenants"), ["*"]),
        prov_roots=_list(claims.get("prov_roots")),
        prov_principals=_list(claims.get("prov_principals")),
        prov_actions=claims.get("prov_actions", "*"),
        prov_resources=claims.get("prov_resources", "*"),
        prov_namespace=str(claims.get("prov_namespace") or ""),
        aip=_list(claims.get("aip")),
        raw=claims,
    )


def authorize_integration(
    ctx: IntegrationContext,
    *,
    client_ip: str,
    enforce_aip: bool = True,
    extra_ip_allowlist: Optional[list[str]] = None,
) -> None:
    """Gate a provisioning request. Raises :class:`AuthError` on failure.

    1. Must be an integration-service token with the ``provisioning`` capability.
    2. Source-IP defense-in-depth (§3.3): the derived client IP must be within the
       token's ``aip`` allow-list (empty ⇒ that gate is disabled) AND within any
       deployment backstop allow-list.
    """
    if not ctx.integration_id:
        raise AuthError("no integration identity", 401, "unauthorized")
    if not ctx.is_integration():
        raise AuthError("not an integration-service token with provisioning capability",
                        403, "not_integration")
    if enforce_aip:
        if not netutil.ip_allowed(client_ip, ctx.aip):
            raise AuthError(f"source IP {client_ip} not in the integration allow-list",
                            403, "ip_not_allowed")
        if extra_ip_allowlist and not netutil.ip_allowed(client_ip, extra_ip_allowlist):
            raise AuthError(f"source IP {client_ip} not in the deployment allow-list",
                            403, "ip_not_allowed")
