# Copyright (C) 2026 James Hickman
# AGPL-3.0-or-later — see package header.
"""Dependency-injection container (SPEC §7). Bundles the collaborators the API
router needs so tests can inject fakes and production uses the real impls."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .audit import Audit
from .core import Core
from .engine import Actions, Resources
from .stores import Store
from .tenant import TenantValidator


@dataclass
class Providers:
    store: Store
    resources: Resources
    actions: Actions
    make_core: Callable[[str, str], Core]   # (integration_id, tenant) -> Core
    audit: Audit
    tenant_validator: TenantValidator


def default_providers(config) -> Providers:
    """Production wiring: Postgres store, folder_actions orchestrators, gRPC core as
    the integration principal, Redis audit, LDAP tenant validation. Heavy deps import
    lazily (psycopg/httpx/redis/ldap3 on first use)."""
    from .audit import RedisAudit
    from .core import GrpcCore
    from .orchestrators import FolderActionsActions, FolderActionsResources
    from .stores import PgStore
    from .tenant import AllowAllTenantValidator, LdapTenantValidator

    validator = (LdapTenantValidator(config) if config.enforce_tenant_ldap
                 else AllowAllTenantValidator())
    return Providers(
        store=PgStore(config),
        resources=FolderActionsResources(config),
        actions=FolderActionsActions(config),
        make_core=lambda integration_id, tenant: GrpcCore(
            config, integration_id=integration_id, tenant=tenant),
        audit=RedisAudit(config),
        tenant_validator=validator,
    )
