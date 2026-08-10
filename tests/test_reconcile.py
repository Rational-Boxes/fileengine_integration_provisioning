# Copyright (C) 2026 James Hickman — AGPL-3.0-or-later. See <https://www.gnu.org/licenses/>.
"""Drift reconciliation sweep tests (reconcile.py)."""

from fakes import FakeCore
from provisioning_service.stores import InMemoryStore
from provisioning_service import reconcile as R


def _core_with_space(integration_id="acme", version="1"):
    """A FakeCore holding a healthy 3-node provisioned space (root + two children)."""
    core = FakeCore()
    root = core.make_dir(core.root_uid(), "ACME Space")
    core.set_metadata(root, "managed_by", integration_id)
    core.set_metadata(root, "provision.version", version)
    core.set_metadata(root, "provision.integration_id", integration_id)
    approved = core.make_dir(root, "Approved")
    core.set_metadata(approved, "managed_by", integration_id)
    drafts = core.make_dir(root, "Drafts")
    core.set_metadata(drafts, "managed_by", integration_id)
    space = {
        "tenant": "t", "integration_id": integration_id, "version": version,
        "external_id": "ext-1", "space_uid": root,
        "nodes": [{"path": ".", "uid": root},
                  {"path": "Approved", "uid": approved},
                  {"path": "Drafts", "uid": drafts}],
    }
    return core, space, {"root": root, "approved": approved, "drafts": drafts}


def test_healthy_space_has_no_drift():
    core, space, _ = _core_with_space()
    assert R.reconcile_space(core, space) == []


def test_missing_space_root_reported_once():
    core, space, uids = _core_with_space()
    # Root vanishes entirely (metadata gone) -> single MISSING_SPACE, no child noise.
    core.nodes.pop(uids["root"])
    findings = R.reconcile_space(core, space)
    assert len(findings) == 1
    assert findings[0].kind == R.DRIFT_MISSING_SPACE
    assert findings[0].space_uid == uids["root"]


def test_missing_child_node_reported():
    core, space, uids = _core_with_space()
    core.nodes.pop(uids["approved"])
    findings = R.reconcile_space(core, space)
    assert [f.kind for f in findings] == [R.DRIFT_MISSING_NODE]
    assert findings[0].path == "Approved"
    assert findings[0].uid == uids["approved"]


def test_ownership_drift_on_child():
    core, space, uids = _core_with_space()
    core.set_metadata(uids["drafts"], "managed_by", "someone-else")
    findings = R.reconcile_space(core, space)
    assert [f.kind for f in findings] == [R.DRIFT_OWNERSHIP]
    assert findings[0].path == "Drafts"


def test_ownership_drift_on_root():
    core, space, uids = _core_with_space()
    core.set_metadata(uids["root"], "managed_by", "hijacker")
    kinds = [f.kind for f in R.reconcile_space(core, space)]
    assert R.DRIFT_OWNERSHIP in kinds


def test_version_drift_on_root():
    core, space, uids = _core_with_space(version="1")
    core.set_metadata(uids["root"], "provision.version", "2")  # core re-stamped to a newer version
    findings = R.reconcile_space(core, space)
    assert [f.kind for f in findings] == [R.DRIFT_VERSION]
    assert "!= persisted '1'" in findings[0].detail


def _seed(store, core, tenant, integration_id, external_id, root_name, status="applied"):
    """Build a 2-node space (root + one child) in `core` and seed it into `store`."""
    root = core.make_dir(core.root_uid(), root_name)
    core.set_metadata(root, "managed_by", integration_id)
    core.set_metadata(root, "provision.version", "1")
    core.set_metadata(root, "provision.integration_id", integration_id)
    child = core.make_dir(root, "Approved")
    core.set_metadata(child, "managed_by", integration_id)
    key = (tenant, integration_id, external_id)
    store.spaces[key] = {"tenant": tenant, "integration_id": integration_id, "version": "1",
                         "external_id": external_id, "space_uid": root, "status": status, "id": len(store.spaces) + 1}
    store.nodes[key] = [{"path": ".", "uid": root}, {"path": "Approved", "uid": child}]
    return root, child


def test_reconcile_tenant_sweeps_all_active_spaces():
    # One core -> unique uids across both spaces (independent FakeCores would collide).
    store, core = InMemoryStore(), FakeCore()
    _seed(store, core, "t", "acme", "ext-a", "Space A")            # healthy
    _, b_child = _seed(store, core, "t", "acme", "ext-b", "Space B")
    core.nodes.pop(b_child)                                        # drift only in B

    findings = R.reconcile_tenant(store, lambda i, t: core, "t")
    assert [f.kind for f in findings] == [R.DRIFT_MISSING_NODE]
    assert findings[0].external_id == "ext-b"


def test_reconcile_tenant_skips_deleted_spaces():
    store, core = InMemoryStore(), FakeCore()
    root, _ = _seed(store, core, "t", "acme", "ext-1", "Gone", status="deleted")
    core.nodes.pop(root)  # would be MISSING_SPACE if the sweep didn't skip deleted
    assert R.reconcile_tenant(store, lambda i, t: core, "t") == []
