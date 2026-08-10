# Copyright (C) 2026 James Hickman
# AGPL-3.0-or-later — see package header.
"""Persistence (SPEC §4) behind a small ``Store`` protocol so the API router is
testable against an in-memory fake. ``PgStore`` is the real psycopg implementation
(integration-tested against Postgres); ``InMemoryStore`` backs unit tests."""
from __future__ import annotations

import json
from typing import Any, Optional, Protocol, runtime_checkable

from . import schema


@runtime_checkable
class Store(Protocol):
    def ensure_tenant(self, tenant: str) -> None: ...
    def find_space(self, tenant: str, integration_id: str, external_id: str) -> Optional[dict]: ...
    def persist(self, tenant: str, req, result) -> dict: ...


def _space_row(req, result) -> dict:
    return {
        "external_id": req.external_id,
        "integration_id": req.integration_id,
        "tenant": req.tenant,
        "space_uid": result.space_uid,
        "blueprint_name": result.blueprint_name,
        "version": result.version,
        "params": req.params,
        "status": result.status,
    }


class InMemoryStore:
    """Dict-backed store for tests. Idempotent by (tenant, integration_id, external_id)."""

    def __init__(self):
        self.spaces: dict[tuple, dict] = {}
        self.nodes: dict[tuple, list] = {}
        self.bindings: dict[tuple, list] = {}
        self.resources: dict[tuple, dict] = {}
        self.runs: list[dict] = []

    def ensure_tenant(self, tenant: str) -> None:  # no-op in memory
        return None

    def _key(self, tenant, integration_id, external_id):
        return (tenant, integration_id, external_id)

    def find_space(self, tenant, integration_id, external_id):
        return self.spaces.get(self._key(tenant, integration_id, external_id))

    def persist(self, tenant, req, result) -> dict:
        key = self._key(tenant, req.integration_id, req.external_id)
        row = _space_row(req, result)
        existed = key in self.spaces
        row["id"] = self.spaces[key]["id"] if existed else len(self.spaces) + 1
        self.spaces[key] = row
        self.nodes[key] = list(result.nodes)
        self.bindings[key] = list(result.actions)
        for r in result.resources:
            rk = (tenant, req.namespace, r["type"], r["name"])
            rec = self.resources.get(rk, {"ref_count": 0})
            rec.update({"service_object_id": r.get("id"), "managed_by": req.integration_id})
            rec["ref_count"] = rec.get("ref_count", 0) + (0 if existed else 1)
            self.resources[rk] = rec
        self.runs.append({"space_id": row["id"], "mode": req.mode, "dry_run": req.dry_run,
                          "outcome": result.status, "actor": req.integration_id})
        return row


class PgStore:
    """Real psycopg store. Thin; integration-tested against Postgres (not unit tests)."""

    def __init__(self, config):
        self.config = config
        self._pool = None

    def _conn(self):
        import psycopg  # lazy
        if self._pool is None:
            c = self.config
            self._pool = psycopg.connect(
                host=c.pg_host, port=c.pg_port, user=c.pg_user,
                password=c.pg_password, dbname=c.pg_database, autocommit=False)
        return self._pool

    def ensure_tenant(self, tenant: str) -> None:
        schema.ensure_tenant_schema(self._conn(), tenant)

    def find_space(self, tenant, integration_id, external_id):
        s = schema.schema_name(tenant)
        with self._conn().cursor() as cur:
            cur.execute(
                f'SELECT id, space_uid, blueprint_name, version, status '
                f'FROM "{s}".provisioned_space WHERE integration_id=%s AND external_id=%s',
                (integration_id, external_id))
            r = cur.fetchone()
        if not r:
            return None
        return {"id": r[0], "space_uid": r[1], "blueprint_name": r[2],
                "version": r[3], "status": r[4]}

    def persist(self, tenant, req, result) -> dict:
        s = schema.schema_name(tenant)
        conn = self._conn()
        row = _space_row(req, result)
        with conn.cursor() as cur:
            cur.execute(
                f'INSERT INTO "{s}".provisioned_space '
                f'(external_id, integration_id, tenant, space_uid, blueprint_name, version, '
                f' last_blueprint, params, status, last_applied_at) '
                f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, now()) "
                f'ON CONFLICT (integration_id, external_id) DO UPDATE SET '
                f' space_uid=EXCLUDED.space_uid, version=EXCLUDED.version, '
                f' last_blueprint=EXCLUDED.last_blueprint, params=EXCLUDED.params, '
                f' status=EXCLUDED.status, last_applied_at=now() RETURNING id',
                (req.external_id, req.integration_id, req.tenant, result.space_uid,
                 result.blueprint_name, result.version, json.dumps(req.blueprint),
                 json.dumps(req.params), result.status))
            space_id = cur.fetchone()[0]
            for n in result.nodes:
                if n.get("uid"):
                    cur.execute(
                        f'INSERT INTO "{s}".provisioned_node (space_id, path, uid) '
                        f'VALUES (%s,%s,%s) ON CONFLICT (space_id, path) DO UPDATE SET uid=EXCLUDED.uid',
                        (space_id, n["path"], n["uid"]))
            for a in result.actions:
                cur.execute(
                    f'INSERT INTO "{s}".provisioned_binding (space_id, ref, folder_uid, fa_binding_id, type) '
                    f'VALUES (%s,%s,%s,%s,%s) ON CONFLICT (space_id, ref) DO UPDATE SET '
                    f' folder_uid=EXCLUDED.folder_uid, fa_binding_id=EXCLUDED.fa_binding_id, updated_at=now()',
                    (space_id, a.get("ref"), a.get("folder_uid"), a.get("binding_id"), a.get("type", "")))
            cur.execute(
                f'INSERT INTO "{s}".apply_run (space_id, mode, dry_run, outcome, report, actor) '
                f'VALUES (%s,%s,%s,%s,%s,%s)',
                (space_id, req.mode, req.dry_run, result.status,
                 json.dumps({"nodes": result.nodes, "actions": result.actions,
                             "resources": result.resources, "warnings": result.warnings}),
                 req.integration_id))
        conn.commit()
        row["id"] = space_id
        return row
