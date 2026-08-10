# Copyright (C) 2026 James Hickman
# AGPL-3.0-or-later — see package header.
"""Dependency-injection container (SPEC §7). Bundles the collaborators the API
router needs so tests can inject fakes and production uses the real impls."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .core import Core
from .engine import Actions, Resources
from .stores import Store


@dataclass
class Providers:
    store: Store
    resources: Resources
    actions: Actions
    make_core: Callable[[str, str], Core]   # (integration_id, tenant) -> Core


def default_providers(config) -> Providers:
    """Production wiring: Postgres store, folder_actions orchestrators, gRPC core as
    the integration principal. Heavy deps import lazily (psycopg/httpx on first use)."""
    from .core import GrpcCore
    from .orchestrators import FolderActionsActions, FolderActionsResources
    from .stores import PgStore

    return Providers(
        store=PgStore(config),
        resources=FolderActionsResources(config),
        actions=FolderActionsActions(config),
        make_core=lambda integration_id, tenant: GrpcCore(
            config, integration_id=integration_id, tenant=tenant),
    )
