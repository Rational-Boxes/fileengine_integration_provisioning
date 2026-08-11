# Copyright (C) 2026 James Hickman
# AGPL-3.0-or-later — see package header.
"""Apply engine (SPEC §7) — turn a blueprint into a provisioned space.

Apply order is **resources → folders → actions** (§7): tenant-scoped resources first
(yielding the ids that ``${resource:...}`` references resolve to), then the folder tree
+ ACL/metadata (yielding the fresh uids that ``${node:...}`` references resolve to),
then the automation bindings. Idempotent + reconcilable; never destructive by default;
stamps ``provision.*`` + the well-known ``managed_by`` marker (§5.6/§5.7).

Composes three collaborators, each a small protocol so the engine is unit-testable
against fakes: :class:`~provisioning_service.core.Core`, ``Resources``, ``Actions``."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from . import blueprint as bp
from .core import Core


class Resources(Protocol):
    """Create/reconcile a tenant-scoped dependent resource (§5.8), returning its
    service object id."""
    def apply(self, *, tenant: str, namespace: str, rtype: str, name: str,
              body: dict, managed_by: str) -> str: ...


class Actions(Protocol):
    """Create/reconcile a folder_actions binding (§7.1), returning its binding id."""
    def apply(self, *, folder_uid: str, ref: str, atype: str, on_events: list,
              mime_types: list, config: dict, managed_by: str) -> str: ...


def _default_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ApplyRequest:
    tenant: str
    external_id: str
    version: str
    blueprint: dict
    params: dict = field(default_factory=dict)
    parent_uid: Optional[str] = None
    mode: str = "reconcile"           # create | reconcile | enforce
    dry_run: bool = False
    integration_id: str = ""
    namespace: str = ""


@dataclass
class ApplyResult:
    external_id: str
    blueprint_name: str
    version: str
    status: str                       # created | reconciled | planned
    space_uid: Optional[str] = None
    nodes: list = field(default_factory=list)      # {path, uid, action}
    actions: list = field(default_factory=list)    # {ref, folder_uid, binding_id, action}
    resources: list = field(default_factory=list)  # {ref, type, name, id, action}
    warnings: list = field(default_factory=list)


class Engine:
    def __init__(self, core: Core, resources: Resources, actions: Actions,
                 *, max_nodes: int = 500, max_depth: int = 12, now=_default_now):
        self.core = core
        self.resources = resources
        self.actions = actions
        self.max_nodes = max_nodes
        self.max_depth = max_depth
        self._now = now

    # ---- public ----
    def apply(self, req: ApplyRequest) -> ApplyResult:
        doc = req.blueprint
        bp.validate_or_raise(doc, max_nodes=self.max_nodes, max_depth=self.max_depth)
        missing = bp.missing_required_params(doc, req.params)
        if missing:
            raise bp.BlueprintError([f"missing required param: {m}" for m in missing])
        values = bp.with_defaults(doc, req.params)
        managed_by = req.integration_id
        dry = req.dry_run

        result = ApplyResult(
            external_id=req.external_id,
            blueprint_name=doc.get("name", ""),
            version=req.version,
            status="planned" if dry else "reconciled",
        )

        # 1) resources → ref -> service object id
        res_map: dict[str, str] = {}
        for r in doc.get("resources") or []:
            name = bp.resolve_params(r.get("name", ""), values)
            body = bp.resolve_params(r.get("body") or {}, values)
            if dry:
                result.resources.append({"ref": r["ref"], "type": r["type"],
                                         "name": name, "id": None, "action": "plan"})
                res_map[r["ref"]] = f"<resource:{r['ref']}>"
                continue
            oid = self.resources.apply(
                tenant=req.tenant, namespace=req.namespace, rtype=r["type"],
                name=name, body=body, managed_by=managed_by)
            res_map[r["ref"]] = oid
            result.resources.append({"ref": r["ref"], "type": r["type"],
                                     "name": name, "id": oid, "action": "applied"})

        # 2) folder tree → address -> uid (+ ACL + metadata + provision stamps)
        node_map: dict[str, Optional[str]] = {}
        root = doc["root"]
        parent = req.parent_uid or self.core.root_uid()
        self._apply_node(root, parent, (), node_map, values, managed_by, req, result,
                         dry, is_root=True)
        result.space_uid = node_map.get(root.get("name", "")) or node_map.get(".")

        # 3) actions (node/resource refs now resolvable)
        for a in doc.get("actions") or []:
            folder_uid = node_map.get(a.get("folder", ""))
            entry = {"ref": a.get("ref"), "folder_uid": folder_uid}
            if dry:
                entry["action"] = "plan"
                result.actions.append(entry)
                continue
            cfg = bp.resolve_params(a.get("config") or {}, values)
            cfg = _resolve_refs(cfg, node_map, res_map)
            bid = self.actions.apply(
                folder_uid=folder_uid, ref=a["ref"], atype=a["type"],
                on_events=a.get("on_events") or [], mime_types=a.get("mime_types") or [],
                config=cfg, managed_by=managed_by)
            entry["binding_id"] = bid
            entry["action"] = "applied"
            result.actions.append(entry)

        return result

    # ---- internal ----
    def _apply_node(self, node, parent_uid, written_prefix, node_map, values,
                    managed_by, req, result, dry, *, is_root):
        written_name = node.get("name", "")
        real_name = bp.resolve_params(written_name, values)

        existing = self.core.child_by_name(parent_uid, real_name)
        if dry:
            uid = existing  # may be None (would be created)
            action = "existing" if existing else "create"
        elif existing:
            uid, action = existing, "existing"
        else:
            uid, action = self.core.make_dir(parent_uid, real_name), "created"

        # register addresses: written relative path, root written-name + '.'/'/'
        if is_root:
            for key in (written_name, ".", "/"):
                node_map[key] = uid
            addr = written_name
        else:
            addr = "/".join(written_prefix + (written_name,))
            node_map[addr] = uid
        if node.get("ref"):
            node_map[node["ref"]] = uid

        result.nodes.append({"path": addr, "uid": uid, "action": action})

        if not dry and uid:
            self._stamp(node, uid, values, managed_by, req, is_root)

        rel = () if is_root else written_prefix + (written_name,)
        for child in node.get("children") or []:
            self._apply_node(child, uid, rel, node_map, values, managed_by, req,
                             result, dry, is_root=False)

    def _stamp(self, node, uid, values, managed_by, req, is_root):
        # metadata (§5.2) — additive/overwrite (reconcile & enforce both set)
        for k, v in (node.get("metadata") or {}).items():
            self.core.set_metadata(uid, k, bp.resolve_params(v, values))
        # well-known managed marker on every folder (§5.7)
        self.core.set_metadata(uid, "managed_by", managed_by)
        if is_root:  # provision.* version stamp (§5.6)
            self.core.set_metadata(uid, "provision.name", req.blueprint.get("name", ""))
            self.core.set_metadata(uid, "provision.version", req.version)
            self.core.set_metadata(uid, "provision.integration_id", req.integration_id)
            self.core.set_metadata(uid, "provision.external_id", req.external_id)
            self.core.set_metadata(uid, "provision.applied_at", self._now())
        # ACLs (§5.2) — additive; principals param-substituted
        existing_acls = self.core.get_acls(uid) if req.mode == "enforce" else None
        for entry in node.get("acls") or []:
            principal = bp.resolve_params(entry.get("principal", ""), values)
            for effect in ("allow", "deny"):
                for perm in entry.get(effect) or []:
                    if existing_acls is not None and _has_acl(existing_acls, principal, perm, effect):
                        continue
                    self.core.grant(uid, principal, perm, effect)


def _has_acl(acls: list[dict], principal: str, perm: str, effect: str) -> bool:
    return any(a.get("principal") == principal and a.get("perm") == perm
              and a.get("effect") == effect for a in acls)


def _resolve_refs(value: Any, node_map: dict, res_map: dict) -> Any:
    """Replace ``${node:X}`` / ``${resource:Y}`` tokens with resolved uid/id.
    A whole-string token yields the raw value (so a uid isn't stringly-embedded);
    inline tokens are string-substituted."""
    if isinstance(value, str):
        m = bp._TOKEN.fullmatch(value)
        if m:
            kind, target = bp._classify(m.group(1))
            if kind == "node" and target in node_map:
                return node_map[target]
            if kind == "resource" and target in res_map:
                return res_map[target]
            return value

        def _sub(mo):
            kind, target = bp._classify(mo.group(1))
            if kind == "node" and target in node_map:
                return str(node_map[target])
            if kind == "resource" and target in res_map:
                return str(res_map[target])
            return mo.group(0)
        return bp._TOKEN.sub(_sub, value)
    if isinstance(value, dict):
        return {k: _resolve_refs(v, node_map, res_map) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_refs(v, node_map, res_map) for v in value]
    return value
