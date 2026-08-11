# Copyright (C) 2026 James Hickman
# AGPL-3.0-or-later — see package header.
"""End-to-end auth chain through FastAPI (SPEC §3): bearer → verify → IP → gate."""
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("jwt")
import jwt  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from provisioning_service.app import create_app  # noqa: E402
from provisioning_service.config import Config  # noqa: E402

SECRET = "unit-test-shared-secret-value-32b!"


def _app(env=None):
    base = {"FILEENGINE_JWT_SECRET": SECRET}
    base.update(env or {})
    return TestClient(create_app(Config(env=base)))


def _tok(**over):
    claims = {
        "sub": "acme-crm", "tenant": "t1", "amr": ["integration"],
        "capabilities": ["provisioning"], "prov_namespace": "acme",
        "aip": [], "exp": int(time.time()) + 300,
    }
    claims.update(over)
    return jwt.encode(claims, SECRET, algorithm="HS256")


def _auth(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_whoami_ok_with_valid_integration_token():
    r = _app().get("/v1/provisioning/whoami", headers=_auth(_tok()))
    assert r.status_code == 200
    assert r.json()["integration_id"] == "acme-crm"
    assert r.json()["namespace"] == "acme"


def test_missing_token_401():
    assert _app().get("/v1/provisioning/whoami").status_code == 401


def test_bad_signature_401():
    bad = jwt.encode({"sub": "x", "exp": int(time.time()) + 60}, "wrong", algorithm="HS256")
    assert _app().get("/v1/provisioning/whoami", headers=_auth(bad)).status_code == 401


def test_delegated_token_403():
    r = _app().get("/v1/provisioning/whoami", headers=_auth(_tok(amr=["delegated"])))
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "not_integration"


def test_offlist_ip_403():
    # aip restricts to a CIDR the TestClient host ('testclient') can't match → reject.
    r = _app().get("/v1/provisioning/whoami",
                   headers=_auth(_tok(aip=["198.51.100.0/24"])))
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "ip_not_allowed"
