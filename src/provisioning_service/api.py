# Copyright (C) 2026 James Hickman
# AGPL-3.0-or-later — see package header.
"""Provisioning API router (SPEC §6). ``POST /v1/provisioning/spaces`` applies an
inline blueprint via the engine, persists the result, and returns the report."""
from __future__ import annotations

import dataclasses
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from . import audit as au
from . import blueprint as bp
from .auth import IntegrationContext
from .config import Config
from .deps import get_config, request_client_ip, require_integration
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
    providers = request.app.state.providers
    src_ip = request_client_ip(request, cfg)

    def _reject(status: int, detail: dict):
        providers.audit.emit(au.REJECTED, integration_id=ctx.integration_id,
                             tenant=body.tenant, external_id=body.external_id,
                             outcome=detail.get("code"), source_ip=src_ip)
        raise HTTPException(status, detail)

    try:
        _scope_check(ctx, body)
    except HTTPException as e:
        _reject(e.status_code, e.detail)

    # Tenant model (§3.2): the tenant's LDAP OU must exist (external app inits it first);
    # the core materializes the schema/storage lazily on the first write.
    if not body.dry_run:
        if not providers.tenant_validator.is_valid(body.tenant):
            _reject(409, {"code": "tenant_not_initialized", "tenant": body.tenant})
        providers.store.ensure_tenant(body.tenant)

    existing = providers.store.find_space(body.tenant, ctx.integration_id, body.external_id)
    if body.mode == "create" and existing and not body.dry_run:
        _reject(409, {"code": "already_exists", "external_id": body.external_id})

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
        _reject(422, {"code": "invalid_blueprint", "errors": e.errors})

    if not body.dry_run:
        providers.store.persist(body.tenant, req, result)
        providers.audit.emit(
            au.space_event(result.status), integration_id=ctx.integration_id,
            tenant=body.tenant, blueprint_name=result.blueprint_name,
            version=result.version, space_uid=result.space_uid,
            external_id=body.external_id, mode=body.mode, outcome=result.status,
            source_ip=src_ip)

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


@router.get("/spaces/{space_uid}")
def inspect_space(
    space_uid: str, tenant: str, request: Request,
    ctx: IntegrationContext = Depends(require_integration),
) -> dict[str, Any]:
    if not ctx.tenant_allowed(tenant):
        raise HTTPException(403, {"code": "tenant_not_allowed"})
    rec = request.app.state.providers.store.get_space_by_uid(tenant, space_uid)
    if not rec or rec.get("integration_id") not in (None, ctx.integration_id):
        raise HTTPException(404, {"code": "not_found"})
    return rec


@router.delete("/spaces/{space_uid}")
def delete_space(
    space_uid: str, tenant: str, request: Request,
    ctx: IntegrationContext = Depends(require_integration),
    cfg: Config = Depends(get_config),
) -> dict[str, Any]:
    if not cfg.allow_space_delete:
        raise HTTPException(403, {"code": "delete_disabled"})
    if not ctx.tenant_allowed(tenant):
        raise HTTPException(403, {"code": "tenant_not_allowed"})
    providers = request.app.state.providers
    ok = providers.store.soft_delete_space(tenant, space_uid)
    if not ok:
        raise HTTPException(404, {"code": "not_found"})
    providers.audit.emit(au.SPACE_DELETED, integration_id=ctx.integration_id,
                        tenant=tenant, space_uid=space_uid,
                        source_ip=request_client_ip(request, cfg))
    return {"status": "deleted", "space_uid": space_uid}


class ResourceBody(BaseModel):
    tenant: str
    type: str
    name: str
    body: dict = Field(default_factory=dict)


@router.post("/resources")
def apply_resource(
    rb: ResourceBody, request: Request,
    ctx: IntegrationContext = Depends(require_integration),
    cfg: Config = Depends(get_config),
) -> dict[str, Any]:
    if not ctx.tenant_allowed(rb.tenant):
        raise HTTPException(403, {"code": "tenant_not_allowed"})
    if not ctx.resource_allowed(rb.type):
        raise HTTPException(403, {"code": "resource_not_allowed", "type": rb.type})
    providers = request.app.state.providers
    providers.store.ensure_tenant(rb.tenant)
    oid = providers.resources.apply(
        tenant=rb.tenant, namespace=ctx.prov_namespace, rtype=rb.type,
        name=rb.name, body=rb.body, managed_by=ctx.integration_id)
    providers.store.apply_resource(rb.tenant, ctx.prov_namespace, rb.type, rb.name,
                                   oid, ctx.integration_id)
    providers.audit.emit(au.RESOURCE_APPLIED, integration_id=ctx.integration_id,
                        tenant=rb.tenant, type=rb.type, name=rb.name,
                        source_ip=request_client_ip(request, cfg))
    return {"type": rb.type, "name": rb.name, "service_object_id": oid, "status": "applied"}


@router.delete("/resources/{rtype}/{name}")
def delete_resource(
    rtype: str, name: str, tenant: str, request: Request,
    ctx: IntegrationContext = Depends(require_integration),
    force: bool = False,
) -> dict[str, Any]:
    if not ctx.tenant_allowed(tenant):
        raise HTTPException(403, {"code": "tenant_not_allowed"})
    store = request.app.state.providers.store
    ok = store.delete_resource(tenant, ctx.prov_namespace, rtype, name, force=force)
    if not ok:
        rec = store.find_resource(tenant, ctx.prov_namespace, rtype, name)
        if rec is None:
            raise HTTPException(404, {"code": "not_found"})
        raise HTTPException(409, {"code": "resource_in_use",
                                  "ref_count": rec.get("ref_count")})
    return {"status": "deleted", "type": rtype, "name": name}
