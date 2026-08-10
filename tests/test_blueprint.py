# Copyright (C) 2026 James Hickman
# AGPL-3.0-or-later — see package header.
"""Unit tests for the pure blueprint logic (SPEC §5)."""
import pytest

from provisioning_service import blueprint as bp


def _valid_doc():
    return {
        "name": "project-standard",
        "params": {
            "project_code": {"type": "string", "required": True},
            "manager_role": {"type": "principal", "required": True},
            "member_role": {"type": "principal", "required": True},
            "classifier_set": {"type": "ref"},
            "webhook_context": {"type": "map"},
            "callback_url": {"type": "url"},
        },
        "root": {
            "name": "${project_code}",
            "metadata": {"type": "project", "code": "${project_code}"},
            "acls": [
                {"principal": "role:${manager_role}", "allow": ["r", "w", "d", "m"]},
                {"principal": "role:${member_role}", "allow": ["r", "w"]},
                {"principal": "everyone", "deny": ["r"]},
            ],
            "children": [
                {"name": "Documents"},
                {"name": "Drawings", "children": [{"name": "Superseded"}]},
                {"name": "Incoming", "children": [{"name": "Rejected"}]},
                {"name": "Approved"},
            ],
        },
        "resources": [
            {"type": "classifier_set", "ref": "mfg", "name": "${project_code}-mfg",
             "body": {"classifiers": []}},
        ],
        "actions": [
            {"ref": "callback", "folder": "Incoming", "type": "webhook",
             "config": {"url": "${callback_url}", "context": "${webhook_context}"}},
            {"ref": "route-approved", "folder": "Incoming", "type": "move_review",
             "config": {"on_approved": "${node:Approved}",
                        "on_rejected": "${node:Incoming/Rejected}"}},
            {"ref": "auto-sort", "folder": "${project_code}", "type": "sorter",
             "config": {"classifier_set_id": "${resource:mfg}"}},
        ],
    }


# --- addressing -------------------------------------------------------------

def test_node_addresses_root_and_relative_paths():
    addrs = bp.node_addresses(_valid_doc()["root"])
    assert "${project_code}" in addrs          # root by its written name
    assert "Approved" in addrs                 # child, relative
    assert "Incoming/Rejected" in addrs        # nested, relative path
    assert "Drawings/Superseded" in addrs
    assert "Documents" in addrs


def test_explicit_ref_is_addressable():
    root = {"name": "X", "children": [{"name": "A", "ref": "alpha"}]}
    addrs = bp.node_addresses(root)
    assert "A" in addrs and "alpha" in addrs


def test_tree_stats():
    count, depth = bp.tree_stats(_valid_doc()["root"])
    # root, Documents, Drawings, Superseded, Incoming, Rejected, Approved
    assert count == 7
    assert depth == 2   # Drawings/Superseded and Incoming/Rejected


# --- validation happy path --------------------------------------------------

def test_valid_blueprint_passes():
    assert bp.validate(_valid_doc()) == []


def test_valid_with_allowed_gates():
    doc = _valid_doc()
    errs = bp.validate(
        doc,
        allowed_actions={"webhook", "move_review", "sorter", "notify", "raise_review"},
        allowed_resources={"classifier_set", "notify_template"},
    )
    assert errs == []


# --- validation failures ----------------------------------------------------

def test_dangling_node_ref_rejected():
    doc = _valid_doc()
    doc["actions"][1]["config"]["on_approved"] = "${node:DoesNotExist}"
    errs = bp.validate(doc)
    assert any("DoesNotExist" in e for e in errs)


def test_dangling_resource_ref_rejected():
    doc = _valid_doc()
    doc["actions"][2]["config"]["classifier_set_id"] = "${resource:ghost}"
    errs = bp.validate(doc)
    assert any("ghost" in e for e in errs)


def test_unknown_param_token_rejected():
    doc = _valid_doc()
    doc["root"]["metadata"]["x"] = "${not_a_param}"
    errs = bp.validate(doc)
    assert any("not_a_param" in e for e in errs)


def test_action_folder_must_be_a_node():
    doc = _valid_doc()
    doc["actions"][0]["folder"] = "Nowhere"
    errs = bp.validate(doc)
    assert any("Nowhere" in e for e in errs)


def test_unknown_action_type_rejected_when_gated():
    doc = _valid_doc()
    doc["actions"][0]["type"] = "frobnicate"
    errs = bp.validate(doc, allowed_actions={"webhook", "sorter", "move_review"})
    assert any("frobnicate" in e for e in errs)


def test_unknown_resource_type_rejected_when_gated():
    doc = _valid_doc()
    doc["resources"][0]["type"] = "mystery"
    errs = bp.validate(doc, allowed_resources={"classifier_set"})
    assert any("mystery" in e for e in errs)


def test_bad_permission_letter_rejected():
    doc = _valid_doc()
    doc["root"]["acls"][0]["allow"] = ["r", "ZZZ"]
    errs = bp.validate(doc)
    assert any("ZZZ" in e for e in errs)


def test_bad_param_type_rejected():
    doc = _valid_doc()
    doc["params"]["project_code"]["type"] = "wat"
    errs = bp.validate(doc)
    assert any("project_code" in e for e in errs)


def test_over_depth_rejected():
    doc = _valid_doc()
    errs = bp.validate(doc, max_depth=1)
    assert any("too deep" in e for e in errs)


def test_over_nodes_rejected():
    doc = _valid_doc()
    errs = bp.validate(doc, max_nodes=3)
    assert any("too large" in e for e in errs)


def test_missing_root_rejected():
    assert any("root" in e for e in bp.validate({"params": {}}))


def test_validate_or_raise():
    doc = _valid_doc()
    doc["actions"][0]["folder"] = "Nowhere"
    with pytest.raises(bp.BlueprintError) as ei:
        bp.validate_or_raise(doc)
    assert ei.value.errors


# --- token extraction -------------------------------------------------------

def test_iter_tokens_classifies():
    kinds = set(bp.iter_tokens(_valid_doc()))
    assert ("node", "Approved") in kinds
    assert ("resource", "mfg") in kinds
    assert ("param", "project_code") in kinds


# --- param substitution -----------------------------------------------------

def test_resolve_params_scalar_interpolation():
    out = bp.resolve_params("${project_code}-mfg", {"project_code": "ACME"})
    assert out == "ACME-mfg"


def test_resolve_params_whole_value_map_injection():
    ctx = {"tenant": "t", "corr": 42}
    out = bp.resolve_params("${webhook_context}", {"webhook_context": ctx})
    assert out == ctx  # structured value returned whole, not stringified


def test_resolve_params_leaves_node_and_resource_tokens():
    out = bp.resolve_params(
        {"on_approved": "${node:Approved}", "cs": "${resource:mfg}"},
        {"project_code": "ACME"},
    )
    assert out["on_approved"] == "${node:Approved}"
    assert out["cs"] == "${resource:mfg}"


def test_resolve_params_recurses_dict_and_list():
    out = bp.resolve_params(
        {"a": ["${x}", {"b": "${x}"}]}, {"x": "V"})
    assert out == {"a": ["V", {"b": "V"}]}


def test_missing_required_params():
    doc = _valid_doc()
    miss = bp.missing_required_params(doc, {"project_code": "ACME"})
    assert set(miss) == {"manager_role", "member_role"}


def test_with_defaults():
    doc = {"params": {"a": {"type": "string", "default": "D"}, "b": {"type": "string"}}}
    merged = bp.with_defaults(doc, {"b": "B"})
    assert merged == {"a": "D", "b": "B"}
    # supplied value overrides default
    assert bp.with_defaults(doc, {"a": "override"})["a"] == "override"
