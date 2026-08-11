# Copyright (C) 2026 James Hickman
# AGPL-3.0-or-later — see package header.
"""InMemoryStore behavior (SPEC §4). PgStore is integration-tested separately."""
from provisioning_service.engine import ApplyRequest, ApplyResult
from provisioning_service.stores import InMemoryStore, Store


def _req(external_id="p1"):
    return ApplyRequest(tenant="t1", external_id=external_id, version="1",
                        blueprint={"name": "bp"}, params={"a": 1},
                        integration_id="acme", namespace="acme")


def _result(space_uid="uid-1"):
    return ApplyResult(external_id="p1", blueprint_name="bp", version="1",
                       status="reconciled", space_uid=space_uid,
                       nodes=[{"path": "X", "uid": space_uid, "action": "created"}],
                       actions=[{"ref": "a", "folder_uid": space_uid, "binding_id": "b", "type": "sorter"}],
                       resources=[{"ref": "r", "type": "classifier_set", "name": "acme/x", "id": "cs:1"}])


def test_inmemory_satisfies_protocol():
    assert isinstance(InMemoryStore(), Store)


def test_find_none_then_persist_then_find():
    s = InMemoryStore()
    assert s.find_space("t1", "acme", "p1") is None
    row = s.persist("t1", _req(), _result())
    assert row["space_uid"] == "uid-1" and row["id"] == 1
    got = s.find_space("t1", "acme", "p1")
    assert got["space_uid"] == "uid-1"


def test_persist_idempotent_reuses_id_and_refcount_once():
    s = InMemoryStore()
    r1 = s.persist("t1", _req(), _result())
    r2 = s.persist("t1", _req(), _result())          # same external_id
    assert r1["id"] == r2["id"]                       # same space row
    rk = ("t1", "acme", "classifier_set", "acme/x")
    assert s.resources[rk]["ref_count"] == 1          # counted once, not twice
    assert len(s.runs) == 2                           # but two apply_runs logged


def test_persist_records_nodes_and_bindings():
    s = InMemoryStore()
    s.persist("t1", _req(), _result())
    key = ("t1", "acme", "p1")
    assert s.nodes[key][0]["uid"] == "uid-1"
    assert s.bindings[key][0]["binding_id"] == "b"


def test_get_space_by_uid_and_soft_delete():
    s = InMemoryStore()
    s.persist("t1", _req(), _result("uid-9"))
    got = s.get_space_by_uid("t1", "uid-9")
    assert got and got["nodes"] and got["bindings"]
    assert s.get_space_by_uid("t1", "missing") is None
    assert s.soft_delete_space("t1", "uid-9") is True
    assert s.find_space("t1", "acme", "p1")["status"] == "deleted"
    assert s.soft_delete_space("t1", "missing") is False


def test_resource_apply_find_delete_refcount_guard():
    s = InMemoryStore()
    s.apply_resource("t1", "acme", "classifier_set", "shared", "cs:1", "acme")
    assert s.find_resource("t1", "acme", "classifier_set", "shared")["service_object_id"] == "cs:1"
    # ref-counted → blocked without force
    s.resources[("t1", "acme", "classifier_set", "shared")]["ref_count"] = 2
    assert s.delete_resource("t1", "acme", "classifier_set", "shared") is False
    assert s.delete_resource("t1", "acme", "classifier_set", "shared", force=True) is True
    assert s.find_resource("t1", "acme", "classifier_set", "shared") is None
