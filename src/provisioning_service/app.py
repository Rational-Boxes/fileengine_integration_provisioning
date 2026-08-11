# Copyright (C) 2026 James Hickman
# AGPL-3.0-or-later — see package header.
"""FastAPI application factory + entrypoint (SPEC §6, §11).

P0 skeleton: boots, serves loopback-gated monitoring endpoints. Auth, the apply
engine, and the API routers land in later phases (P1–P4)."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from .auth import IntegrationContext
from .config import Config
from .deps import require_integration
from .providers import Providers, default_providers

log = logging.getLogger("provisioning_service")

_MONITOR_PATHS = {"/healthz", "/readyz", "/poolz"}


def create_app(config: Optional[Config] = None,
               providers: Optional[Providers] = None) -> FastAPI:
    cfg = config or Config()
    app = FastAPI(title="FileEngine Provisioning Service", version="0.1.0")
    app.state.config = cfg
    # Collaborators (store/orchestrators/core factory) — injectable for tests.
    app.state.providers = providers or default_providers(cfg)

    # Monitoring endpoints are unauthenticated → restrict to loopback / allow-ips
    # (monitoring-port-binding convention).
    allow = set(cfg.monitoring_allow_ips)

    @app.middleware("http")
    async def _guard_monitoring(request: Request, call_next):
        if request.url.path in _MONITOR_PATHS:
            client = request.client.host if request.client else ""
            if allow and client not in allow:
                return JSONResponse({"error": "forbidden"}, status_code=403)
        return await call_next(request)

    if cfg.cors_origins:  # normally empty — server-to-server API
        from fastapi.middleware.cors import CORSMiddleware
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cfg.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "service": "provisioning", "version": "0.1.0"}

    @app.get("/readyz")
    async def readyz():
        # P0: liveness only. Later phases check DB + core reachability.
        return {"status": "ready"}

    # First authenticated route — proves the integration-service auth chain
    # (bearer → HS256 verify → client-IP derivation → capability + aip gate, §3).
    @app.get("/v1/provisioning/whoami")
    async def whoami(ctx: IntegrationContext = Depends(require_integration)):
        return {
            "integration_id": ctx.integration_id,
            "tenant": ctx.tenant,
            "namespace": ctx.prov_namespace,
            "capabilities": ctx.capabilities,
        }

    return create_app_finalize(app)


def create_app_finalize(app: FastAPI) -> FastAPI:
    from .api import router as provisioning_router
    app.include_router(provisioning_router)
    return app


def factory() -> FastAPI:  # for ``uvicorn provisioning_service.app:factory --factory``
    return create_app()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config()
    app = create_app(cfg)
    log.info("provisioning service starting on %s:%s (grpc=%s, fa=%s)",
             cfg.http_host, cfg.http_port, cfg.grpc_address, cfg.folder_actions_url)
    import uvicorn
    uvicorn.run(app, host=cfg.http_host, port=cfg.http_port)


if __name__ == "__main__":  # pragma: no cover
    main()
