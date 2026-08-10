# Copyright (C) 2026 James Hickman
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License as published by the Free
# Software Foundation, either version 3 of the License, or (at your option) any
# later version. See <https://www.gnu.org/licenses/>.

"""Drift reconciliation sweep (optional; the ``provisioning-service-reconcile`` console
script).

Read-only: compares each persisted provisioned space against the live Core metadata
(``managed_by`` + ``provision.*`` stamps written at apply time) and reports drift — a
space root or folder that has vanished, been taken over by another owner, or carries a
different provision version than what was persisted. It never mutates anything; a
follow-up remediation pass (re-stamp / re-apply the blueprint) is a separate concern.

The detection logic (:func:`reconcile_space` / :func:`reconcile_tenant`) is pure and
protocol-injected, so it is unit-tested with the same fakes as the apply engine.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable, Optional

DRIFT_MISSING_SPACE = "missing_space"     # the space root folder is gone
DRIFT_MISSING_NODE = "missing_node"       # a provisioned child folder is gone
DRIFT_OWNERSHIP = "ownership_drift"       # managed_by no longer matches the integration
DRIFT_VERSION = "version_drift"           # root provision.version != persisted version


@dataclass
class Drift:
    tenant: str
    space_uid: str
    external_id: str
    integration_id: str
    kind: str
    detail: str
    uid: Optional[str] = None
    path: Optional[str] = None


def _live_meta(core, uid: str) -> Optional[dict]:
    """Live metadata for a node, or None when the node is gone/unreachable. An empty
    metadata dict is treated as gone — a provisioned node always carries ``managed_by``."""
    try:
        meta = core.get_metadata(uid)
    except Exception:
        return None
    return meta if meta else None


def reconcile_space(core, space: dict) -> list[Drift]:
    """Check one persisted space against live Core metadata; return the drift found."""
    tenant = space.get("tenant", "")
    integration_id = space.get("integration_id", "")
    version = space.get("version", "")
    external_id = space.get("external_id", "")
    space_uid = space.get("space_uid", "")

    def mk(kind: str, detail: str, uid: Optional[str] = None, path: Optional[str] = None) -> Drift:
        return Drift(tenant, space_uid, external_id, integration_id, kind, detail, uid, path)

    root_meta = _live_meta(core, space_uid)
    if root_meta is None:
        # Root gone: nothing else is checkable, and reporting every child as missing
        # would be noise. One finding is the actionable signal.
        return [mk(DRIFT_MISSING_SPACE, f"space root {space_uid} not found in core")]

    findings: list[Drift] = []
    if root_meta.get("managed_by") != integration_id:
        findings.append(mk(DRIFT_OWNERSHIP,
                           f"root managed_by={root_meta.get('managed_by')!r} != {integration_id!r}",
                           uid=space_uid))
    root_ver = root_meta.get("provision.version")
    if version and root_ver is not None and root_ver != version:
        findings.append(mk(DRIFT_VERSION,
                           f"root provision.version={root_ver!r} != persisted {version!r}",
                           uid=space_uid))

    for node in space.get("nodes", []):
        uid = node.get("uid")
        path = node.get("path")
        if not uid or uid == space_uid:
            continue  # root already checked
        meta = _live_meta(core, uid)
        if meta is None:
            findings.append(mk(DRIFT_MISSING_NODE, f"node {path!r} ({uid}) not found in core",
                               uid=uid, path=path))
        elif meta.get("managed_by") != integration_id:
            findings.append(mk(DRIFT_OWNERSHIP,
                               f"node {path!r} managed_by={meta.get('managed_by')!r} != {integration_id!r}",
                               uid=uid, path=path))
    return findings


def reconcile_tenant(store, make_core: Callable[[str, str], Any], tenant: str) -> list[Drift]:
    """Sweep every active provisioned space in a tenant. Cores are created per
    integration id (the apply principal) and reused across that integration's spaces."""
    findings: list[Drift] = []
    cores: dict[str, Any] = {}
    for summary in store.list_spaces(tenant):
        if summary.get("status") == "deleted":
            continue
        space = store.get_space_by_uid(tenant, summary.get("space_uid"))
        if not space:
            continue
        integration_id = space.get("integration_id", "")
        if integration_id not in cores:
            cores[integration_id] = make_core(integration_id, tenant)
        findings.extend(reconcile_space(cores[integration_id], space))
    return findings


def main(argv: Optional[list] = None) -> int:
    """CLI entry point. Tenants come from argv (positional) or PROV_RECONCILE_TENANTS.
    Prints a JSON drift report and exits non-zero when drift is found."""
    import json
    import sys
    from .config import Config
    from .providers import default_providers

    args = list(sys.argv[1:] if argv is None else argv)
    tenants = [a for a in args if not a.startswith("-")]
    config = Config()
    if not tenants:
        raw = getattr(config, "reconcile_tenants", "") or ""
        tenants = [t.strip() for t in raw.split(",") if t.strip()]

    providers = default_providers(config)
    findings: list[Drift] = []
    for tenant in tenants:
        findings.extend(reconcile_tenant(providers.store, providers.make_core, tenant))

    print(json.dumps({"tenants": tenants, "drift_count": len(findings),
                      "drift": [asdict(f) for f in findings]}, indent=2))
    return 1 if findings else 0
