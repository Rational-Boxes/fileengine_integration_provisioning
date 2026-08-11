# Copyright (C) 2026 James Hickman
# AGPL-3.0-or-later — see package header.
"""FastAPI dependencies: extract + verify the bearer, derive the client IP, and
authorize (SPEC §3). Thin glue over the pure logic in ``auth.py`` / ``netutil.py`` /
``jwt_verify.py``."""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from . import netutil
from .auth import AuthError, IntegrationContext, authorize_integration, context_from_claims
from .config import Config
from .jwt_verify import TokenError, verify_bridge_token


def get_config(request: Request) -> Config:
    return request.app.state.config


def bearer_token(request: Request) -> str:
    h = request.headers.get("authorization", "")
    if not h.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    return h[7:].strip()


def request_client_ip(request: Request, cfg: Config) -> str:
    remote = request.client.host if request.client else ""
    xff = request.headers.get("x-forwarded-for")
    return netutil.derive_client_ip(remote, xff, cfg.trusted_proxies)


def require_integration(
    request: Request,
    cfg: Config = Depends(get_config),
    token: str = Depends(bearer_token),
) -> IntegrationContext:
    """Authenticated + authorized integration-service context, or an HTTP error.

    (Bridge-introspection fallback for a missing shared secret is a later increment;
    for now local HS256 verify is required.)"""
    try:
        claims = verify_bridge_token(token, cfg.jwt_secret)
    except TokenError as e:
        raise HTTPException(status_code=401, detail=str(e))
    ctx = context_from_claims(claims)
    client_ip = request_client_ip(request, cfg)
    try:
        authorize_integration(
            ctx,
            client_ip=client_ip,
            enforce_aip=cfg.enforce_aip,
            extra_ip_allowlist=cfg.ip_allowlist or None,
        )
    except AuthError as e:
        raise HTTPException(status_code=e.status, detail={"code": e.code, "message": e.message})
    return ctx
