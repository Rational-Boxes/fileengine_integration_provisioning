# Copyright (C) 2026 James Hickman — AGPL-3.0-or-later. See <https://www.gnu.org/licenses/>.
"""GET/PATCH /v1/provisioning/spaces/{uid}/config (SPEC §6.3)."""
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("jwt")

from test_api import _harness, _tok, _body  # reuse the API harness + token/body builders


def _apply(client):
    r = client.post("/v1/provisioning/spaces", json=_body(), headers=_tok())
    assert r.status_code == 200, r.text
    return r.json()["space_uid"]


def test_get_config_returns_actions_and_params():
    client, prov, core = _harness()
    uid = _apply(client)
    r = client.get(f"/v1/provisioning/spaces/{uid}/config?tenant=t1", headers=_tok())
    assert r.status_code == 200, r.text
    cfg = r.json()
    assert cfg["space_uid"] == uid
    assert "route" in [a["ref"] for a in cfg["actions"]]
    assert cfg["params"]["code"] == "ACME"          # not a secret -> present


def test_get_config_scope_denied_and_not_found():
    client, prov, core = _harness()
    uid = _apply(client)
    r = client.get(f"/v1/provisioning/spaces/{uid}/config?tenant=t1",
                   headers=_tok(prov_tenants=["other"]))
    assert r.status_code == 403
    r = client.get("/v1/provisioning/spaces/does-not-exist/config?tenant=t1", headers=_tok())
    assert r.status_code == 404


# A blueprint whose patchable param feeds only an action's config (an automation param),
# not the folder structure — the real §6.3 use case (webhook context, notify recipients).
def _auto_doc():
    return {
        "name": "std",
        "params": {"code": {"type": "string", "required": True},
                   "notify_to": {"type": "string", "required": False, "default": "ops@x"}},
        "root": {"name": "${code}", "children": [{"name": "Incoming"}]},
        "actions": [{"ref": "n", "folder": "Incoming", "type": "notify",
                     "config": {"to": "${notify_to}"}}],
    }


def test_patch_config_merges_automation_params_in_place():
    client, prov, core = _harness()
    r = client.post("/v1/provisioning/spaces",
                    json={"tenant": "t1", "external_id": "auto-1", "version": "1",
                          "blueprint": _auto_doc(), "params": {"code": "ACME", "notify_to": "a@x"}},
                    headers=_tok())
    assert r.status_code == 200, r.text
    uid = r.json()["space_uid"]
    before = len(core.calls)

    # Patch only the automation param — the root name (${code}) is unchanged, so the
    # space stays in place (same uid) and the notify binding is re-rendered.
    r = client.patch(f"/v1/provisioning/spaces/{uid}/config",
                     json={"tenant": "t1", "params": {"notify_to": "b@x"}}, headers=_tok())
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["space_uid"] == uid                   # in place, not relocated
    assert out["params"]["notify_to"] == "b@x"       # merged
    assert any(a.get("action") == "applied" for a in out["actions"])
    assert len(core.calls) > before                  # reconcile drove core work


def test_patch_config_rejects_disallowed_action_scope():
    client, prov, core = _harness()
    uid = _apply(client)
    r = client.patch(f"/v1/provisioning/spaces/{uid}/config",
                     json={"tenant": "t1", "params": {}}, headers=_tok(prov_actions=["other_action"]))
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "action_not_allowed"


def test_patch_config_unknown_space_404():
    client, prov, core = _harness()
    r = client.patch("/v1/provisioning/spaces/nope/config",
                     json={"tenant": "t1", "params": {}}, headers=_tok())
    assert r.status_code == 404
