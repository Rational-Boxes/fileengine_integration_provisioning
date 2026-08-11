# Copyright (C) 2026 James Hickman
# AGPL-3.0-or-later — see package header.
"""Tenant validation (SPEC §3.2). A tenant is defined by its OU in the shared LDAP
directory (Posture B); the external app initializes it *before* FileEngine is touched.
Provisioning validates it exists, then adopts it (the core materializes the tenant
schema/storage lazily on the first write). Behind a protocol so the router is testable."""
from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

log = logging.getLogger("provisioning_service.tenant")


@runtime_checkable
class TenantValidator(Protocol):
    def is_valid(self, tenant: str) -> bool: ...


class AllowAllTenantValidator:
    """Validation disabled (dev/test, or LDAP unavailable). Accepts any tenant."""

    def is_valid(self, tenant: str) -> bool:
        return bool(tenant)


class LdapTenantValidator:
    """Confirms ``ou=<tenant>`` exists under the tenant base in the shared LDAP."""

    def __init__(self, config):
        self.config = config

    def is_valid(self, tenant: str) -> bool:
        if not tenant:
            return False
        try:
            import ldap3  # lazy
            server = ldap3.Server(self.config.ldap_endpoint)
            conn = ldap3.Connection(server, auto_bind=True)
            try:
                dn = f"ou={tenant},{self.config.ldap_tenant_base}"
                # existence check — search the exact OU entry
                return conn.search(dn, "(objectClass=*)",
                                   search_scope=ldap3.BASE, attributes=["ou"])
            finally:
                conn.unbind()
        except Exception:
            log.warning("LDAP tenant validation failed for %s — treating as invalid",
                        tenant, exc_info=True)
            return False
