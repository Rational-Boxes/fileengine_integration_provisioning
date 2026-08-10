# Copyright (C) 2026 James Hickman
# AGPL-3.0-or-later — see package header.
"""Config env parsing (SPEC §9)."""
from provisioning_service.config import Config


def test_defaults():
    c = Config(env={})
    assert c.http_port == 8100
    assert c.pg_database == "provisioning"
    assert c.grpc_address == "localhost:50051"
    assert c.enforce_aip is True                    # defense-in-depth on by default
    assert c.allow_space_delete is True             # decision 4
    assert c.trusted_proxies == ["127.0.0.1", "::1"]
    assert c.cors_origins == []                     # server-to-server, no browser CORS


def test_overrides():
    c = Config(env={
        "PROV_HTTP_PORT": "9100",
        "PROV_ENFORCE_AIP": "false",
        "PROV_TRUSTED_PROXIES": "10.0.0.1, 10.0.0.2",
        "PROV_MAX_TREE_DEPTH": "20",
        "FILEENGINE_JWT_SECRET": "s3cret",
        "PROV_FOLDER_ACTIONS_URL": "http://fa:8099",
    })
    assert c.http_port == 9100
    assert c.enforce_aip is False
    assert c.trusted_proxies == ["10.0.0.1", "10.0.0.2"]
    assert c.max_tree_depth == 20
    assert c.jwt_secret == "s3cret"
    assert c.folder_actions_url == "http://fa:8099"


def test_bad_int_falls_back_to_default():
    c = Config(env={"PROV_HTTP_PORT": "not-a-number"})
    assert c.http_port == 8100
