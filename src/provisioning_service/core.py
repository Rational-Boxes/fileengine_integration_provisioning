# Copyright (C) 2026 James Hickman
# AGPL-3.0-or-later — see package header.
"""Core access as the integration principal (SPEC §3, §7).

The apply engine talks to the core through the small :class:`Core` protocol, so it is
testable against a fake (``tests/fakes.py``). ``GrpcCore`` is the real implementation
over ``python_interface`` — it acts **as the integration's service identity**
(``user=integration_id``, roles ``[provisioning_role]``, the request tenant), so every
write is ACL-checked by the core against the grants the integration holds on its scoped
root (least-privilege, §3)."""
from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

# Permission letter -> proto Permission enum NAME (the platform ACL vocabulary).
PERM_NAME = {
    "r": "READ", "w": "WRITE", "d": "DELETE", "l": "LIST_DELETED", "u": "UNDELETE",
    "v": "VIEW_VERSIONS", "b": "RETRIEVE_BACK_VERSION", "s": "RESTORE_TO_VERSION",
    "x": "EXECUTE", "m": "MANAGE_ACL", "i": "ACL_INHERIT", "CULL_VERSIONS": "CULL_VERSIONS",
}


@runtime_checkable
class Core(Protocol):
    """The core operations the apply engine needs (as the integration principal)."""

    def root_uid(self) -> str: ...
    def child_by_name(self, parent_uid: str, name: str) -> Optional[str]: ...
    def make_dir(self, parent_uid: str, name: str) -> str: ...
    def set_metadata(self, uid: str, key: str, value: str) -> None: ...
    def get_metadata(self, uid: str) -> dict: ...
    def grant(self, uid: str, principal: str, perm_letter: str, effect: str = "allow") -> None: ...
    def revoke(self, uid: str, principal: str, perm_letter: str, effect: str = "allow") -> None: ...
    def get_acls(self, uid: str) -> list[dict]: ...


class GrpcCore:
    """Real :class:`Core` over ``python_interface``, acting as the integration."""

    def __init__(self, config, *, integration_id: str, tenant: str):
        self.config = config
        self.integration_id = integration_id
        self.tenant = tenant
        self.role = config.provisioning_role
        self._mf = None

    def _client(self):
        if self._mf is None:
            from ._client import load
            ManagedFiles, _err, _nf = load()
            self._mf = ManagedFiles(
                server_address=self.config.grpc_address,
                user_name=self.integration_id,
                user_roles=[self.role],
                tenant=self.tenant,
            )
        return self._mf

    # -- reads --
    def root_uid(self) -> str:
        # 'root' aliases the tenant root in the bridge/core.
        return "root"

    def child_by_name(self, parent_uid: str, name: str) -> Optional[str]:
        try:
            for e in self._client().list_directory(parent_uid, tenant=self.tenant) or []:
                if getattr(e, "name", None) == name:
                    return getattr(e, "uid", None)
        except Exception:
            return None
        return None

    def get_metadata(self, uid: str) -> dict:
        try:
            return self._client().get_metadata_values(uid, tenant=self.tenant) or {}
        except Exception:
            return {}

    def get_acls(self, uid: str) -> list[dict]:
        try:
            return list(self._client().get_resource_acls(uid, tenant=self.tenant) or [])
        except Exception:
            return []

    # -- writes (as the integration principal) --
    def make_dir(self, parent_uid: str, name: str) -> str:
        return self._client().make_directory(parent_uid, name, tenant=self.tenant)

    def set_metadata(self, uid: str, key: str, value: str) -> None:
        self._client().set_metadata_value(uid, key, str(value), tenant=self.tenant)

    def grant(self, uid: str, principal: str, perm_letter: str, effect: str = "allow") -> None:
        self._client().grant_permission(
            uid, principal, PERM_NAME.get(perm_letter, perm_letter),
            effect=effect, tenant=self.tenant)

    def revoke(self, uid: str, principal: str, perm_letter: str, effect: str = "allow") -> None:
        self._client().revoke_permission(
            uid, principal, PERM_NAME.get(perm_letter, perm_letter),
            effect=effect, tenant=self.tenant)
