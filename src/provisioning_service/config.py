# Copyright (C) 2026 James Hickman
# AGPL-3.0-or-later — see package header.
"""Configuration from environment (SPEC §9). Shared platform keys keep the
``FILEENGINE_*`` prefix; service-private keys use ``PROV_*``.

Pure: reading env only, no I/O — so it is import-safe and unit-testable via the
optional ``env`` override."""
from __future__ import annotations

import os
from typing import Mapping, Optional


def _env(env: Mapping[str, str], key: str, default: str = "") -> str:
    return env.get(key, default)


def _int(env: Mapping[str, str], key: str, default: int) -> int:
    try:
        return int(env.get(key, "") or default)
    except ValueError:
        return default


def _bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    v = env.get(key, "")
    if v == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def _csv(env: Mapping[str, str], key: str) -> list[str]:
    return [s.strip() for s in env.get(key, "").split(",") if s.strip()]


class Config:
    """Effective service configuration. Instantiate with ``Config()`` (reads
    ``os.environ``) or ``Config(env=...)`` in tests."""

    def __init__(self, env: Optional[Mapping[str, str]] = None) -> None:
        e = os.environ if env is None else env

        # --- shared platform ---
        self.grpc_host = _env(e, "FILEENGINE_GRPC_HOST", "localhost")
        self.grpc_port = _int(e, "FILEENGINE_GRPC_PORT", 50051)
        self.redis_host = _env(e, "FILEENGINE_REDIS_HOST", "localhost")
        self.redis_port = _int(e, "FILEENGINE_REDIS_PORT", 6379)
        self.redis_password = _env(e, "FILEENGINE_REDIS_PASSWORD")
        self.redis_db = _int(e, "FILEENGINE_REDIS_DB", 0)
        self.audit_stream = _env(e, "FILEENGINE_AUDIT_STREAM", "fileengine:audit")
        self.jwt_secret = _env(e, "FILEENGINE_JWT_SECRET")
        self.ldap_endpoint = _env(e, "FILEENGINE_LDAP_ENDPOINT", "ldap://localhost:1389")
        self.ldap_tenant_base = _env(e, "FILEENGINE_LDAP_TENANT_BASE",
                                     "ou=tenants,dc=rationalboxes,dc=com")

        # --- HTTP ---
        self.http_host = _env(e, "PROV_HTTP_HOST", "127.0.0.1")
        self.http_port = _int(e, "PROV_HTTP_PORT", 8100)
        self.cors_origins = _csv(e, "PROV_CORS_ORIGINS")
        # Default tenants for the reconcile drift sweep when none are passed on the CLI.
        self.reconcile_tenants = _env(e, "PROV_RECONCILE_TENANTS", "")

        # --- Postgres (own DB) ---
        self.pg_host = _env(e, "PROV_PG_HOST", "localhost")
        self.pg_port = _int(e, "PROV_PG_PORT", 5434)
        self.pg_user = _env(e, "PROV_PG_USER", "postgres")
        self.pg_password = _env(e, "PROV_PG_PASSWORD", "postgres")
        self.pg_database = _env(e, "PROV_PG_DATABASE", "provisioning")
        self.db_statement_timeout_ms = _int(e, "PROV_DB_STATEMENT_TIMEOUT_MS", 15000)

        # --- auth + acting identity ---
        self.bridge_url = _env(e, "PROV_BRIDGE_URL", "http://localhost:8090")
        self.bridge_introspect_ttl = _int(e, "PROV_BRIDGE_INTROSPECT_TTL", 30)
        self.provisioning_role = _env(e, "PROV_PROVISIONING_ROLE", "provisioning")
        # Validate the tenant's LDAP OU before adopting (§3.2). Off by default so a
        # dev stack without LDAP still works; enable in real deployments.
        self.enforce_tenant_ldap = _bool(e, "PROV_ENFORCE_TENANT_LDAP", False)

        # --- source-IP enforcement (§3.3) ---
        self.trusted_proxies = _csv(e, "PROV_TRUSTED_PROXIES") or ["127.0.0.1", "::1"]
        self.enforce_aip = _bool(e, "PROV_ENFORCE_AIP", True)
        self.ip_allowlist = _csv(e, "PROV_IP_ALLOWLIST")

        # --- orchestration ---
        self.folder_actions_url = _env(e, "PROV_FOLDER_ACTIONS_URL", "http://localhost:8099")
        self.folder_actions_timeout_s = _int(e, "PROV_FOLDER_ACTIONS_TIMEOUT_S", 15)

        # --- limits / safety ---
        self.max_tree_nodes = _int(e, "PROV_MAX_TREE_NODES", 500)
        self.max_tree_depth = _int(e, "PROV_MAX_TREE_DEPTH", 12)
        self.apply_rate_per_min = _int(e, "PROV_APPLY_RATE_PER_MIN", 60)
        self.allow_space_delete = _bool(e, "PROV_ALLOW_SPACE_DELETE", True)

        # --- monitoring ---
        self.monitoring_allow_ips = _csv(e, "PROV_MONITORING_ALLOW_IPS") or ["127.0.0.1", "::1"]

    @property
    def grpc_address(self) -> str:
        return f"{self.grpc_host}:{self.grpc_port}"
