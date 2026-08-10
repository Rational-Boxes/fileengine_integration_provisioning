# Copyright (C) 2026 James Hickman
# AGPL-3.0-or-later — see package header.
"""Per-tenant Postgres schema (SPEC §4).

Owns its own database; each tenant gets an isolated schema ``tenant_<slug>`` created
idempotently via ``ensure_tenant_schema``. The DDL is a pure ``.format``-templated
string (no braces in comments — they would break ``str.format``)."""
from __future__ import annotations

import re

_SLUG = re.compile(r"[^a-z0-9_]+")


def schema_name(tenant: str) -> str:
    """The Postgres schema for a tenant (mirrors the platform lanes)."""
    slug = _SLUG.sub("_", (tenant or "default").strip().lower()) or "default"
    return f"tenant_{slug}"


_TENANT_DDL = """
CREATE SCHEMA IF NOT EXISTS "{schema}";

-- One row per provisioned space; idempotency + provenance (SPEC 4).
CREATE TABLE IF NOT EXISTS "{schema}".provisioned_space (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    external_id    TEXT NOT NULL,
    integration_id TEXT NOT NULL,
    tenant         TEXT NOT NULL,
    space_uid      TEXT,
    blueprint_name TEXT,
    version        TEXT,
    last_blueprint JSONB,
    params         JSONB,
    status         TEXT NOT NULL DEFAULT 'created',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_applied_at TIMESTAMPTZ,
    UNIQUE (integration_id, external_id)
);

-- Blueprint-path to core uid map for a space; stable across reconciles, and how
-- node:<path> references resolve (SPEC 4 / 7.1).
CREATE TABLE IF NOT EXISTS "{schema}".provisioned_node (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    space_id   BIGINT NOT NULL REFERENCES "{schema}".provisioned_space (id) ON DELETE CASCADE,
    path       TEXT NOT NULL,
    uid        TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'directory',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (space_id, path)
);

-- The space automation map: ref to folder_actions binding id (SPEC 4). No raw secrets.
CREATE TABLE IF NOT EXISTS "{schema}".provisioned_binding (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    space_id      BIGINT NOT NULL REFERENCES "{schema}".provisioned_space (id) ON DELETE CASCADE,
    ref           TEXT NOT NULL,
    folder_uid    TEXT NOT NULL,
    fa_binding_id TEXT,
    type          TEXT NOT NULL,
    config_hash   TEXT,
    secret_refs   JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (space_id, ref)
);

-- Tenant-scoped dependent resources; keyed (namespace, type, name) (SPEC 5.8).
CREATE TABLE IF NOT EXISTS "{schema}".provisioned_resource (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tenant            TEXT NOT NULL,
    namespace         TEXT NOT NULL,
    type              TEXT NOT NULL,
    name              TEXT NOT NULL,
    owning_service    TEXT NOT NULL DEFAULT 'folder_actions',
    service_object_id TEXT,
    config_hash       TEXT,
    managed_by        TEXT,
    ref_count         INTEGER NOT NULL DEFAULT 0,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (namespace, type, name)
);

-- Per-apply log; audit companion (SPEC 4).
CREATE TABLE IF NOT EXISTS "{schema}".apply_run (
    id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    space_id BIGINT,
    mode     TEXT,
    dry_run  BOOLEAN NOT NULL DEFAULT false,
    outcome  TEXT,
    report   JSONB,
    actor    TEXT,
    ts       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_apply_run_space ON "{schema}".apply_run (space_id, ts DESC);
"""


def tenant_ddl(tenant: str) -> str:
    """Idempotent DDL that provisions a tenant's schema + tables."""
    return _TENANT_DDL.format(schema=schema_name(tenant))


def ensure_tenant_schema(conn, tenant: str) -> str:
    """Execute the tenant DDL on ``conn`` (a psycopg connection). Returns the
    schema name. Idempotent."""
    name = schema_name(tenant)
    with conn.cursor() as cur:
        cur.execute(tenant_ddl(tenant))
    conn.commit()
    return name
