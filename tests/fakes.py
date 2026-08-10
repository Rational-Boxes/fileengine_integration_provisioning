# Copyright (C) 2026 James Hickman
# AGPL-3.0-or-later — see package header.
"""In-memory FakeCore implementing the Core protocol — for apply-engine tests.

Simulates a folder tree with uids, per-node metadata and ACLs, and records writes so
tests can assert what provisioning did without a live gRPC core."""
from __future__ import annotations

from typing import Optional


class FakeCore:
    ROOT = "root"

    def __init__(self):
        # uid -> node
        self.nodes: dict[str, dict] = {
            self.ROOT: {"name": "", "parent": None, "children": {}, "meta": {}, "acls": []}
        }
        self._n = 0
        self.calls: list[tuple] = []

    def _new_uid(self) -> str:
        self._n += 1
        return f"uid-{self._n}"

    # -- Core protocol --
    def root_uid(self) -> str:
        return self.ROOT

    def child_by_name(self, parent_uid: str, name: str) -> Optional[str]:
        return self.nodes.get(parent_uid, {}).get("children", {}).get(name)

    def make_dir(self, parent_uid: str, name: str) -> str:
        existing = self.child_by_name(parent_uid, name)
        if existing:
            return existing  # idempotent at the core layer too
        uid = self._new_uid()
        self.nodes[uid] = {"name": name, "parent": parent_uid,
                           "children": {}, "meta": {}, "acls": []}
        self.nodes[parent_uid]["children"][name] = uid
        self.calls.append(("make_dir", parent_uid, name, uid))
        return uid

    def set_metadata(self, uid: str, key: str, value: str) -> None:
        self.nodes[uid]["meta"][key] = str(value)
        self.calls.append(("set_metadata", uid, key, str(value)))

    def get_metadata(self, uid: str) -> dict:
        return dict(self.nodes.get(uid, {}).get("meta", {}))

    def grant(self, uid, principal, perm_letter, effect="allow") -> None:
        self.nodes[uid]["acls"].append((principal, perm_letter, effect))
        self.calls.append(("grant", uid, principal, perm_letter, effect))

    def revoke(self, uid, principal, perm_letter, effect="allow") -> None:
        self.nodes[uid]["acls"] = [
            a for a in self.nodes[uid]["acls"]
            if a != (principal, perm_letter, effect)]
        self.calls.append(("revoke", uid, principal, perm_letter, effect))

    def get_acls(self, uid: str) -> list[dict]:
        return [{"principal": p, "perm": pm, "effect": ef}
                for (p, pm, ef) in self.nodes.get(uid, {}).get("acls", [])]

    # -- test helpers --
    def path_uid(self, *names: str) -> Optional[str]:
        """Resolve a name-path from root to a uid (test convenience)."""
        cur = self.ROOT
        for nm in names:
            cur = self.child_by_name(cur, nm)
            if cur is None:
                return None
        return cur


class FakeResources:
    """Records tenant-scoped resource applies; returns a deterministic id."""

    def __init__(self):
        self.applied: list[dict] = []

    def apply(self, *, tenant, namespace, rtype, name, body, managed_by) -> str:
        self.applied.append({"tenant": tenant, "namespace": namespace, "type": rtype,
                             "name": name, "body": body, "managed_by": managed_by})
        return f"{rtype}:{namespace}/{name}"


class FakeActions:
    """Records folder_actions binding applies (with the fully-resolved config)."""

    def __init__(self):
        self.applied: list[dict] = []

    def apply(self, *, folder_uid, ref, atype, on_events, mime_types, config, managed_by) -> str:
        self.applied.append({"folder_uid": folder_uid, "ref": ref, "type": atype,
                             "on_events": on_events, "mime_types": mime_types,
                             "config": config, "managed_by": managed_by})
        return f"binding-{ref}"

    def by_ref(self, ref: str) -> dict:
        return next(a for a in self.applied if a["ref"] == ref)
