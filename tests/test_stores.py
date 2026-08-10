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
