# Copyright (C) 2026 James Hickman
# AGPL-3.0-or-later — see package header.
"""Persistence (SPEC §4) behind a small ``Store`` protocol so the API router is
testable against an in-memory fake. ``PgStore`` is the real psycopg implementation
(integration-tested against Postgres); ``InMemoryStore`` backs unit tests."""
from __future__ import annotations

import json
import threading
from typing import Any, Optional, Protocol, runtime_checkable

from . import schema


@runtime_checkable
class Store(Protocol):
    def ensure_tenant(self, tenant: str) -> None: ...
    def find_space(self, tenant: str, integration_id: str, external_id: str) -> Optional[dict]: ...
    def list_spaces(self, tenant: str) -> list[dict]: ...
    def persist(self, tenant: str, req, result) -> dict: ...
    def get_space_by_uid(self, tenant: str, space_uid: str) -> Optional[dict]: ...
    def soft_delete_space(self, tenant: str, space_uid: str) -> bool: ...
    def apply_resource(self, tenant: str, namespace: str, rtype: str, name: str,
                       service_object_id: str, managed_by: str) -> dict: ...
    def find_resource(self, tenant: str, namespace: str, rtype: str, name: str) -> Optional[dict]: ...
    def delete_resource(self, tenant: str, namespace: str, rtype: str, name: str,
                        force: bool = False) -> bool: ...


def _space_row(req, result) -> dict:
    return {
        "external_id": req.external_id,
        "integration_id": req.integration_id,
        "tenant": req.tenant,
        "space_uid": result.space_uid,
        "blueprint_name": result.blueprint_name,
        "version": result.version,
        "params": req.params,
        "blueprint": req.blueprint,   # retained for the §6.3 config re-render
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

    def list_spaces(self, tenant):
        return [dict(row) for key, row in self.spaces.items() if key[0] == tenant]

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

    def get_space_by_uid(self, tenant, space_uid):
        for key, row in self.spaces.items():
            if key[0] == tenant and row.get("space_uid") == space_uid:
                return {**row, "nodes": self.nodes.get(key, []),
                        "bindings": self.bindings.get(key, [])}
        return None

    def soft_delete_space(self, tenant, space_uid):
        for key, row in self.spaces.items():
            if key[0] == tenant and row.get("space_uid") == space_uid:
                row["status"] = "deleted"
                return True
        return False

    def apply_resource(self, tenant, namespace, rtype, name, service_object_id, managed_by):
        rk = (tenant, namespace, rtype, name)
        rec = self.resources.get(rk, {"ref_count": 0})
        rec.update({"tenant": tenant, "namespace": namespace, "type": rtype,
                    "name": name, "service_object_id": service_object_id,
                    "managed_by": managed_by})
        self.resources[rk] = rec
        return rec

    def find_resource(self, tenant, namespace, rtype, name):
        return self.resources.get((tenant, namespace, rtype, name))

    def delete_resource(self, tenant, namespace, rtype, name, force=False):
        rk = (tenant, namespace, rtype, name)
        rec = self.resources.get(rk)
        if rec is None:
            return False
        if rec.get("ref_count", 0) > 0 and not force:
            return False
        del self.resources[rk]
        return True


class PgStore:
    """Real psycopg store. Thin; integration-tested against Postgres (not unit tests)."""

    def __init__(self, config):
        self.config = config
        self._pool = None
        # Guards both lazy inits below. Each was check-then-act: two concurrent
        # requests could both find `_pool is None` and open a connection (one
        # then leaks, unreferenced and never closed), and both could decide a
        # tenant needed provisioning and run the same DDL.
        self._lock = threading.Lock()
        self._provisioned: set[str] = set()

    def _conn(self):
        import psycopg  # lazy
        if self._pool is None:
            with self._lock:
                if self._pool is None:
                    c = self.config
                    self._pool = psycopg.connect(
                        host=c.pg_host, port=c.pg_port, user=c.pg_user,
                        password=c.pg_password, dbname=c.pg_database, autocommit=False)
        return self._pool

    def ensure_tenant(self, tenant: str) -> None:
        """Idempotent, and now also memoised: this used to re-run the whole
        tenant DDL on every provisioning request rather than once per tenant."""
        if tenant in self._provisioned:
            return
        conn = self._conn()
        with self._lock:
            if tenant not in self._provisioned:
                schema.ensure_tenant_schema(conn, tenant)
                self._provisioned.add(tenant)

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

    def list_spaces(self, tenant):
        s = schema.schema_name(tenant)
        with self._conn().cursor() as cur:
            cur.execute(
                f'SELECT space_uid, integration_id, external_id, blueprint_name, version, status '
                f'FROM "{s}".provisioned_space')
            rows = cur.fetchall()
        return [{"space_uid": r[0], "integration_id": r[1], "external_id": r[2],
                 "blueprint_name": r[3], "version": r[4], "status": r[5]} for r in rows]

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

    def get_space_by_uid(self, tenant, space_uid):
        s = schema.schema_name(tenant)
        conn = self._conn()
        with conn.cursor() as cur:
            cur.execute(
                f'SELECT id, external_id, integration_id, space_uid, blueprint_name, '
                f'version, status, last_blueprint, params FROM "{s}".provisioned_space '
                f'WHERE space_uid=%s',
                (space_uid,))
            r = cur.fetchone()
            if not r:
                return None
            space_id = r[0]
            cur.execute(f'SELECT path, uid FROM "{s}".provisioned_node WHERE space_id=%s ORDER BY path',
                        (space_id,))
            nodes = [{"path": p, "uid": u} for p, u in cur.fetchall()]
            cur.execute(f'SELECT ref, folder_uid, fa_binding_id, type FROM "{s}".provisioned_binding WHERE space_id=%s',
                        (space_id,))
            bindings = [{"ref": rf, "folder_uid": fu, "binding_id": bid, "type": t}
                        for rf, fu, bid, t in cur.fetchall()]
        return {"id": space_id, "external_id": r[1], "integration_id": r[2],
                "space_uid": r[3], "blueprint_name": r[4], "version": r[5],
                "status": r[6], "blueprint": r[7], "params": r[8] or {},
                "nodes": nodes, "bindings": bindings}

    def soft_delete_space(self, tenant, space_uid):
        s = schema.schema_name(tenant)
        conn = self._conn()
        with conn.cursor() as cur:
            cur.execute(f'UPDATE "{s}".provisioned_space SET status=%s WHERE space_uid=%s',
                        ("deleted", space_uid))
            n = cur.rowcount
        conn.commit()
        return n > 0

    def apply_resource(self, tenant, namespace, rtype, name, service_object_id, managed_by):
        s = schema.schema_name(tenant)
        conn = self._conn()
        with conn.cursor() as cur:
            cur.execute(
                f'INSERT INTO "{s}".provisioned_resource '
                f'(tenant, namespace, type, name, service_object_id, managed_by) '
                f'VALUES (%s,%s,%s,%s,%s,%s) '
                f'ON CONFLICT (namespace, type, name) DO UPDATE SET '
                f' service_object_id=EXCLUDED.service_object_id, updated_at=now() RETURNING id',
                (tenant, namespace, rtype, name, service_object_id, managed_by))
            rid = cur.fetchone()[0]
        conn.commit()
        return {"id": rid, "namespace": namespace, "type": rtype, "name": name,
                "service_object_id": service_object_id}

    def find_resource(self, tenant, namespace, rtype, name):
        s = schema.schema_name(tenant)
        with self._conn().cursor() as cur:
            cur.execute(
                f'SELECT service_object_id, ref_count FROM "{s}".provisioned_resource '
                f'WHERE namespace=%s AND type=%s AND name=%s', (namespace, rtype, name))
            r = cur.fetchone()
        return {"service_object_id": r[0], "ref_count": r[1]} if r else None

    def delete_resource(self, tenant, namespace, rtype, name, force=False):
        s = schema.schema_name(tenant)
        conn = self._conn()
        with conn.cursor() as cur:
            cur.execute(
                f'DELETE FROM "{s}".provisioned_resource '
                f'WHERE namespace=%s AND type=%s AND name=%s AND (ref_count=0 OR %s)',
                (namespace, rtype, name, force))
            n = cur.rowcount
        conn.commit()
        return n > 0
