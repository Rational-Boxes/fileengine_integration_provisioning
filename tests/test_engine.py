# Copyright (C) 2026 James Hickman
# AGPL-3.0-or-later — see package header.
"""Apply engine (SPEC §7) against FakeCore + fake orchestrators."""
import pytest

from provisioning_service import blueprint as bp
from provisioning_service.engine import ApplyRequest, Engine

from fakes import FakeActions, FakeCore, FakeResources


def _doc():
    return {
        "name": "project-standard",
        "params": {
            "project_code": {"type": "string", "required": True},
            "member_role": {"type": "principal", "required": True},
            "classifier_set": {"type": "ref"},
            "webhook_context": {"type": "map"},
        },
        "root": {
            "name": "${project_code}",
            "metadata": {"type": "project", "code": "${project_code}"},
            "acls": [{"principal": "role:${member_role}", "allow": ["r", "w"]}],
            "children": [
                {"name": "Incoming", "children": [{"name": "Rejected"}]},
                {"name": "Approved"},
            ],
        },
        "resources": [
            {"type": "classifier_set", "ref": "mfg", "name": "${project_code}-mfg",
             "body": {"classifiers": []}},
        ],
        "actions": [
            {"ref": "route", "folder": "Incoming", "type": "move_review",
             "on_events": ["review.approved"],
             "config": {"on_approved": "${node:Approved}",
                        "on_rejected": "${node:Incoming/Rejected}"}},
            {"ref": "sort", "folder": "${project_code}", "type": "sorter",
             "config": {"classifier_set_id": "${resource:mfg}",
                        "context": "${webhook_context}"}},
        ],
    }


def _req(**over):
    base = dict(tenant="t1", external_id="proj-42", version="3", blueprint=_doc(),
                params={"project_code": "ACME", "member_role": "eng",
                        "classifier_set": "x", "webhook_context": {"k": "v"}},
                integration_id="acme-crm", namespace="acme")
    base.update(over)
    return ApplyRequest(**base)


def _engine():
    core, res, act = FakeCore(), FakeResources(), FakeActions()
    return Engine(core, res, act), core, res, act


# --- folder tree ------------------------------------------------------------

def test_folders_created_with_substituted_names():
    eng, core, _r, _a = _engine()
    eng.apply(_req())
    root = core.child_by_name("root", "ACME")          # ${project_code} → ACME
    assert root
    assert core.path_uid("ACME", "Incoming", "Rejected")
    assert core.path_uid("ACME", "Approved")


def test_root_stamped_with_provision_and_managed_by():
    eng, core, _r, _a = _engine()
    r = eng.apply(_req())
    meta = core.get_metadata(r.space_uid)
    assert meta["provision.version"] == "3"
    assert meta["provision.name"] == "project-standard"
    assert meta["provision.integration_id"] == "acme-crm"
    assert meta["provision.external_id"] == "proj-42"
    assert meta["managed_by"] == "acme-crm"
    assert meta["type"] == "project" and meta["code"] == "ACME"  # blueprint metadata


def test_every_folder_managed_by():
    eng, core, _r, _a = _engine()
    eng.apply(_req())
    for path in (("ACME",), ("ACME", "Approved"), ("ACME", "Incoming", "Rejected")):
        assert core.get_metadata(core.path_uid(*path))["managed_by"] == "acme-crm"


def test_acls_granted_with_substituted_principal():
    eng, core, _r, _a = _engine()
    eng.apply(_req())
    acls = core.get_acls(core.path_uid("ACME"))
    got = {(a["principal"], a["perm"]) for a in acls}
    assert ("role:eng", "r") in got and ("role:eng", "w") in got


# --- resources + actions ordering & resolution ------------------------------

def test_resources_applied_before_actions_and_resolved():
    eng, core, res, act = _engine()
    r = eng.apply(_req())
    # resource created, namespaced by prov_namespace
    assert res.applied[0]["name"] == "ACME-mfg"
    assert res.applied[0]["namespace"] == "acme"
    assert r.resources[0]["id"] == "classifier_set:acme/ACME-mfg"
    # sorter action's ${resource:mfg} resolved to that id
    sort = act.by_ref("sort")
    assert sort["config"]["classifier_set_id"] == "classifier_set:acme/ACME-mfg"
    # webhook_context param (map) injected whole
    assert sort["config"]["context"] == {"k": "v"}


def test_node_refs_resolved_to_fresh_uids():
    eng, core, res, act = _engine()
    eng.apply(_req())
    route = act.by_ref("route")
    assert route["folder_uid"] == core.path_uid("ACME", "Incoming")
    assert route["config"]["on_approved"] == core.path_uid("ACME", "Approved")
    assert route["config"]["on_rejected"] == core.path_uid("ACME", "Incoming", "Rejected")
    # actions are managed
    assert route["managed_by"] == "acme-crm"


# --- idempotency + modes ----------------------------------------------------

def test_idempotent_reapply_does_not_duplicate_folders():
    eng, core, _r, _a = _engine()
    eng.apply(_req())
    n_mkdir = len([c for c in core.calls if c[0] == "make_dir"])
    eng.apply(_req())  # second apply
    n_mkdir_2 = len([c for c in core.calls if c[0] == "make_dir"])
    assert n_mkdir == n_mkdir_2 == 4   # ACME, Incoming, Rejected, Approved — created once


def test_result_report_shape():
    eng, core, _r, _a = _engine()
    r = eng.apply(_req())
    assert r.status == "reconciled"
    assert r.external_id == "proj-42" and r.version == "3"
    assert {n["action"] for n in r.nodes} == {"created"}
    assert {a["action"] for a in r.actions} == {"applied"}
    assert r.resources[0]["action"] == "applied"


# --- dry_run ----------------------------------------------------------------

def test_dry_run_writes_nothing_and_plans():
    eng, core, res, act = _engine()
    r = eng.apply(_req(dry_run=True))
    assert r.status == "planned"
    assert core.calls == []           # no core writes
    assert res.applied == [] and act.applied == []
    assert {n["action"] for n in r.nodes} == {"create"}   # all would-be-created


# --- validation gates --------------------------------------------------------

def test_missing_required_param_raises():
    eng, _c, _r, _a = _engine()
    with pytest.raises(bp.BlueprintError):
        eng.apply(_req(params={"project_code": "ACME"}))   # member_role missing


def test_invalid_blueprint_raises():
    eng, _c, _r, _a = _engine()
    doc = _doc()
    doc["actions"][0]["config"]["on_approved"] = "${node:Ghost}"
    with pytest.raises(bp.BlueprintError):
        eng.apply(_req(blueprint=doc))
