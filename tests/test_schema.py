# Copyright (C) 2026 James Hickman
# AGPL-3.0-or-later — see package header.
"""Per-tenant DDL generation (SPEC §4)."""
from provisioning_service import schema


def test_schema_name_slugified():
    assert schema.schema_name("default") == "tenant_default"
    assert schema.schema_name("Acme Corp!") == "tenant_acme_corp_"
    assert schema.schema_name("") == "tenant_default"


def test_tenant_ddl_has_all_tables_and_schema():
    ddl = schema.tenant_ddl("filenginetest")
    assert 'CREATE SCHEMA IF NOT EXISTS "tenant_filenginetest"' in ddl
    for tbl in ("provisioned_space", "provisioned_node", "provisioned_binding",
                "provisioned_resource", "apply_run"):
        assert f'"tenant_filenginetest".{tbl}' in ddl
    # idempotency + resource keys present
    assert "UNIQUE (integration_id, external_id)" in ddl
    assert "UNIQUE (namespace, type, name)" in ddl


def test_ddl_has_no_stray_format_braces():
    # every '{' must have been consumed by .format (else .format would have raised,
    # but double-check no literal braces leaked into the SQL).
    ddl = schema.tenant_ddl("t")
    assert "{" not in ddl and "}" not in ddl
