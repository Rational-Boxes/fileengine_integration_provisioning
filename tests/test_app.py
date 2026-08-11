# Copyright (C) 2026 James Hickman
# AGPL-3.0-or-later — see package header.
"""App skeleton smoke tests (SPEC §6.5)."""
import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from provisioning_service.app import create_app  # noqa: E402
from provisioning_service.config import Config  # noqa: E402


def _client(env=None):
    # TestClient's reported client host is 'testclient'; allow-list it for the
    # positive cases (the guard itself is exercised by the negative test).
    base = {"PROV_MONITORING_ALLOW_IPS": "testclient"}
    base.update(env or {})
    return TestClient(create_app(Config(env=base)))


def test_healthz_ok_from_loopback():
    r = _client().get("/healthz")
    assert r.status_code == 200
    assert r.json()["service"] == "provisioning"


def test_readyz_ok():
    assert _client().get("/readyz").status_code == 200


def test_monitoring_blocked_from_non_allowlisted_ip():
    # TestClient reports client host 'testclient'; with an allow-list that excludes it,
    # the monitoring guard must 403.
    c = _client(env={"PROV_MONITORING_ALLOW_IPS": "10.9.9.9"})
    assert c.get("/healthz").status_code == 403
