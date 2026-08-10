# Copyright (C) 2026 James Hickman
# AGPL-3.0-or-later — see package header.
"""End-to-end API router (SPEC §6) with injected fakes."""
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("jwt")
import jwt  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from provisioning_service.app import create_app  # noqa: E402
from provisioning_service.config import Config  # noqa: E402
from provisioning_service.providers import Providers  # noqa: E402
from provisioning_service.stores import InMemoryStore  # noqa: E402

from fakes import FakeActions, FakeCore, FakeResources  # noqa: E402

SECRET = "unit-test-shared-secret-value-32b!"


def _doc():
    return {
        "name": "std", "params": {"code": {"type": "string", "required": True},
                                   "role": {"type": "principal", "required": True}},
        "root": {"name": "${code}", "acls": [{"principal": "role:${role}", "allow": ["r", "w"]}],
                 "children": [{"name": "Incoming"}, {"name": "Approved"}]},
        "resources": [{"type": "classifier_set", "ref": "cs", "name": "${code}-cs", "body": {}}],
        "actions": [{"ref": "route", "folder": "Incoming", "type": "move_review",
                     "config": {"on_approved": "${node:Approved}", "cs": "${resource:cs}"}}],
    }


def _harness(env=None):
    core = FakeCore()
    prov = Providers(store=InMemoryStore(), resources=FakeResources(),
                     actions=FakeActions(), make_core=lambda i, t: core)
    base = {"FILEENGINE_JWT_SECRET": SECRET}
    base.update(env or {})
    client = TestClient(create_app(Config(env=base), providers=prov))
    return client, prov, core


def _tok(**over):
    claims = {"sub": "acme-crm", "tenant": "t1", "amr": ["integration"],
              "capabilities": ["provisioning"], "prov_namespace": "acme",
              "prov_tenants": ["*"], "aip": [], "prov_actions": "*", "prov_resources": "*",
              "exp": int(time.time()) + 300}
    claims.update(over)
    return {"Authorization": "Bearer " + jwt.encode(claims, SECRET, algorithm="HS256")}


def _body(**over):
    b = {"tenant": "t1", "external_id": "proj-1", "version": "3", "blueprint": _doc(),
         "params": {"code": "ACME", "role": "eng"}}
    b.update(over)
    return b


def test_apply_space_ok_end_to_end():
    client, prov, core = _harness()
    r = client.post("/v1/provisioning/spaces", json=_body(), headers=_tok())
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "reconciled" and data["version"] == "3"
    # folders created in the (shared) fake core
    assert core.path_uid("ACME", "Approved")
    # node ref resolved into the action config; resource namespaced + resolved
    route = prov.actions.by_ref("route")
    assert route["config"]["on_approved"] == core.path_uid("ACME", "Approved")
    assert route["config"]["cs"] == "classifier_set:acme/ACME-cs"
    # persisted + idempotent
    assert prov.store.find_space("t1", "acme-crm", "proj-1")["space_uid"] == core.path_uid("ACME")


def test_dry_run_persists_nothing():
    client, prov, core = _harness()
    r = client.post("/v1/provisioning/spaces", json=_body(dry_run=True), headers=_tok())
    assert r.status_code == 200
    assert r.json()["status"] == "planned"
    assert core.calls == []
    assert prov.store.find_space("t1", "acme-crm", "proj-1") is None


def test_invalid_blueprint_422():
    client, _p, _c = _harness()
    bad = _doc(); bad["actions"][0]["config"]["on_approved"] = "${node:Nope}"
    r = client.post("/v1/provisioning/spaces", json=_body(blueprint=bad), headers=_tok())
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "invalid_blueprint"


def test_missing_token_401():
    client, _p, _c = _harness()
    assert client.post("/v1/provisioning/spaces", json=_body()).status_code == 401


def test_tenant_scope_403():
    client, _p, _c = _harness()
    r = client.post("/v1/provisioning/spaces", json=_body(tenant="other"),
                    headers=_tok(prov_tenants=["t1"]))
    assert r.status_code == 403 and r.json()["detail"]["code"] == "tenant_not_allowed"


def test_action_scope_403():
    client, _p, _c = _harness()
    r = client.post("/v1/provisioning/spaces", json=_body(),
                    headers=_tok(prov_actions=["webhook"]))   # move_review not permitted
    assert r.status_code == 403 and r.json()["detail"]["code"] == "action_not_allowed"


def test_create_mode_conflict_409():
    client, _p, _c = _harness()
    client.post("/v1/provisioning/spaces", json=_body(), headers=_tok())     # first
    r = client.post("/v1/provisioning/spaces", json=_body(mode="create"), headers=_tok())
    assert r.status_code == 409 and r.json()["detail"]["code"] == "already_exists"


def test_list_spaces_by_external_id():
    client, _p, _c = _harness()
    client.post("/v1/provisioning/spaces", json=_body(), headers=_tok())
    r = client.get("/v1/provisioning/spaces", params={"tenant": "t1", "external_id": "proj-1"},
                   headers=_tok())
    assert r.status_code == 200 and len(r.json()["spaces"]) == 1
