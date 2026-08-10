# Copyright (C) 2026 James Hickman
# AGPL-3.0-or-later — see package header.
"""Provisioning API router (SPEC §6). ``POST /v1/provisioning/spaces`` applies an
inline blueprint via the engine, persists the result, and returns the report."""
from __future__ import annotations

import dataclasses
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from . import blueprint as bp
from .auth import IntegrationContext
from .config import Config
from .deps import get_config, require_integration
from .engine import ApplyRequest, Engine

router = APIRouter(prefix="/v1/provisioning", tags=["provisioning"])


class ApplySpaceBody(BaseModel):
    tenant: str
    external_id: str
    version: str = ""
    blueprint: dict
    params: dict = Field(default_factory=dict)
    parent_uid: Optional[str] = None
    mode: str = "reconcile"          # create | reconcile | enforce
    dry_run: bool = False


def _scope_check(ctx: IntegrationContext, body: ApplySpaceBody) -> None:
    if not ctx.tenant_allowed(body.tenant):
        raise HTTPException(403, {"code": "tenant_not_allowed", "tenant": body.tenant})
    if ctx.prov_roots and body.parent_uid and body.parent_uid not in ctx.prov_roots:
        raise HTTPException(403, {"code": "root_not_allowed", "parent_uid": body.parent_uid})
    for a in body.blueprint.get("actions") or []:
        if not ctx.action_allowed(a.get("type", "")):
            raise HTTPException(403, {"code": "action_not_allowed", "type": a.get("type")})
    for r in body.blueprint.get("resources") or []:
        if not ctx.resource_allowed(r.get("type", "")):
            raise HTTPException(403, {"code": "resource_not_allowed", "type": r.get("type")})


@router.post("/spaces")
def apply_space(
    body: ApplySpaceBody,
    request: Request,
    ctx: IntegrationContext = Depends(require_integration),
    cfg: Config = Depends(get_config),
) -> dict[str, Any]:
    _scope_check(ctx, body)
    providers = request.app.state.providers

    # TODO(§3.2): validate the tenant's LDAP OU exists (else 409 tenant_not_initialized).
    if not body.dry_run:
        providers.store.ensure_tenant(body.tenant)

    existing = providers.store.find_space(body.tenant, ctx.integration_id, body.external_id)
    if body.mode == "create" and existing and not body.dry_run:
        raise HTTPException(409, {"code": "already_exists", "external_id": body.external_id})

    engine = Engine(
        providers.make_core(ctx.integration_id, body.tenant),
        providers.resources, providers.actions,
        max_nodes=cfg.max_tree_nodes, max_depth=cfg.max_tree_depth)
    req = ApplyRequest(
        tenant=body.tenant, external_id=body.external_id, version=body.version,
        blueprint=body.blueprint, params=body.params, parent_uid=body.parent_uid,
        mode=body.mode, dry_run=body.dry_run,
        integration_id=ctx.integration_id, namespace=ctx.prov_namespace)

    try:
        result = engine.apply(req)
    except bp.BlueprintError as e:
        raise HTTPException(422, {"code": "invalid_blueprint", "errors": e.errors})

    if not body.dry_run:
        providers.store.persist(body.tenant, req, result)

    return dataclasses.asdict(result)


@router.get("/spaces")
def list_spaces(
    tenant: str,
    request: Request,
    ctx: IntegrationContext = Depends(require_integration),
    external_id: Optional[str] = None,
) -> dict[str, Any]:
    if not ctx.tenant_allowed(tenant):
        raise HTTPException(403, {"code": "tenant_not_allowed"})
    store = request.app.state.providers.store
    if external_id:
        rec = store.find_space(tenant, ctx.integration_id, external_id)
        return {"spaces": [rec] if rec else []}
    # full listing is a stores concern (later); for now filter by external_id only.
    return {"spaces": []}
