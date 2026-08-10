# Copyright (C) 2026 James Hickman — AGPL-3.0-or-later. See <https://www.gnu.org/licenses/>.
"""Pure helpers for the per-space config/setup API (§6.3)."""

from provisioning_service import space_config as sc


def test_secret_param_names():
    bp = {"params": {"code": {"type": "string"}, "tok": {"type": "secret"}}}
    assert sc.secret_param_names(bp) == {"tok"}
    assert sc.secret_param_names({}) == set()
    assert sc.secret_param_names({"params": "not-a-dict"}) == set()


def test_redact_params_masks_only_secrets():
    bp = {"params": {"code": {"type": "string"}, "tok": {"type": "secret"}}}
    out = sc.redact_params({"code": "ACME", "tok": "s3cr3t"}, bp)
    assert out == {"code": "ACME", "tok": "***"}
    assert sc.redact_params(None, bp) == {}


def test_merge_params_shallow():
    assert sc.merge_params({"a": 1, "b": 2}, {"b": 3, "c": 4}) == {"a": 1, "b": 3, "c": 4}
    assert sc.merge_params(None, {"x": 1}) == {"x": 1}
    assert sc.merge_params({"x": 1}, None) == {"x": 1}


def test_resolved_config_joins_actions_to_bindings():
    bp = {"params": {"code": {"type": "string"}, "tok": {"type": "secret"}},
          "actions": [{"ref": "route", "folder": "Incoming", "type": "move_review",
                       "on_events": ["upload"], "mime_types": ["application/pdf"]}]}
    space = {"space_uid": "u-root", "external_id": "p1", "version": "3",
             "params": {"code": "ACME", "tok": "s3cr3t"},
             "bindings": [{"ref": "route", "folder_uid": "u-inc", "binding_id": "b1", "type": "move_review"}]}
    cfg = sc.resolved_config(space, bp)
    assert cfg["space_uid"] == "u-root"
    assert cfg["version"] == "3"
    assert cfg["params"] == {"code": "ACME", "tok": "***"}   # secret redacted
    assert cfg["actions"] == [{
        "ref": "route", "type": "move_review", "folder": "Incoming",
        "folder_uid": "u-inc", "binding_id": "b1",
        "on_events": ["upload"], "mime_types": ["application/pdf"]}]


def test_resolved_config_missing_binding_yields_null_fields():
    bp = {"actions": [{"ref": "x", "type": "notify"}]}
    a = sc.resolved_config({"space_uid": "u", "bindings": []}, bp)["actions"][0]
    assert a["binding_id"] is None
    assert a["folder_uid"] is None
