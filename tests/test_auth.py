# Copyright (C) 2026 James Hickman
# AGPL-3.0-or-later — see package header.
"""JWT verify + authorization/scope logic (SPEC §3)."""
import time

import pytest

jwt = pytest.importorskip("jwt")  # PyJWT

from provisioning_service import auth  # noqa: E402
from provisioning_service.jwt_verify import TokenError, verify_bridge_token  # noqa: E402

SECRET = "shared-secret"


def _token(claims, secret=SECRET, exp_in=300, alg="HS256"):
    body = {"exp": int(time.time()) + exp_in, **claims}
    return jwt.encode(body, secret, algorithm=alg)


def _int_claims(**over):
    base = {
        "sub": "acme-crm", "tenant": "t1", "amr": ["integration"],
        "capabilities": ["provisioning"], "prov_tenants": ["*"],
        "prov_namespace": "acme", "aip": ["198.51.100.0/24"],
    }
    base.update(over)
    return base


# --- jwt_verify -------------------------------------------------------------

def test_verify_valid_token():
    claims = verify_bridge_token(_token(_int_claims()), SECRET)
    assert claims["sub"] == "acme-crm"


def test_verify_rejects_wrong_secret():
    with pytest.raises(TokenError):
        verify_bridge_token(_token(_int_claims()), "other-secret")


def test_verify_rejects_expired():
    with pytest.raises(TokenError):
        verify_bridge_token(_token(_int_claims(), exp_in=-10), SECRET)


def test_verify_rejects_alg_none():
    tok = jwt.encode({"sub": "x", "exp": int(time.time()) + 60}, key=None, algorithm="none")
    with pytest.raises(TokenError):
        verify_bridge_token(tok, SECRET)


def test_verify_requires_exp():
    tok = jwt.encode({"sub": "x"}, SECRET, algorithm="HS256")
    with pytest.raises(TokenError):
        verify_bridge_token(tok, SECRET)


def test_verify_requires_secret():
    with pytest.raises(TokenError):
        verify_bridge_token(_token(_int_claims()), "")


# --- context extraction -----------------------------------------------------

def test_context_from_claims():
    ctx = auth.context_from_claims(_int_claims(prov_actions=["webhook"], prov_resources="*"))
    assert ctx.integration_id == "acme-crm"
    assert ctx.is_integration()
    assert ctx.prov_namespace == "acme"
    assert ctx.action_allowed("webhook")
    assert not ctx.action_allowed("sorter")
    assert ctx.resource_allowed("classifier_set")   # "*"


def test_tenant_allowed_wildcard_and_pattern():
    assert auth.context_from_claims(_int_claims(prov_tenants=["*"])).tenant_allowed("anything")
    ctx = auth.context_from_claims(_int_claims(prov_tenants=["acme-*", "beta"]))
    assert ctx.tenant_allowed("beta")          # exact
    assert ctx.tenant_allowed("acme-42")       # prefix* glob
    assert not ctx.tenant_allowed("gamma")


# --- authorization ----------------------------------------------------------

def test_authorize_ok_when_ip_in_aip():
    ctx = auth.context_from_claims(_int_claims(aip=["198.51.100.0/24"]))
    auth.authorize_integration(ctx, client_ip="198.51.100.7")   # no raise


def test_authorize_rejects_offlist_ip():
    ctx = auth.context_from_claims(_int_claims(aip=["198.51.100.0/24"]))
    with pytest.raises(auth.AuthError) as ei:
        auth.authorize_integration(ctx, client_ip="203.0.113.9")
    assert ei.value.code == "ip_not_allowed"


def test_authorize_empty_aip_disables_ip_gate():
    ctx = auth.context_from_claims(_int_claims(aip=[]))
    auth.authorize_integration(ctx, client_ip="203.0.113.9")   # no raise (gate off)


def test_authorize_rejects_non_integration_token():
    ctx = auth.context_from_claims(_int_claims(amr=["delegated"]))
    with pytest.raises(auth.AuthError) as ei:
        auth.authorize_integration(ctx, client_ip="198.51.100.7", enforce_aip=False)
    assert ei.value.code == "not_integration"


def test_authorize_deployment_backstop_allowlist():
    ctx = auth.context_from_claims(_int_claims(aip=[]))  # token gate off
    # but deployment backstop still applies
    with pytest.raises(auth.AuthError):
        auth.authorize_integration(ctx, client_ip="203.0.113.9",
                                   extra_ip_allowlist=["10.0.0.0/8"])
