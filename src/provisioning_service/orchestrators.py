# Copyright (C) 2026 James Hickman
# AGPL-3.0-or-later — see package header.
"""Real Resources/Actions orchestrators over folder_actions' HTTP API (SPEC §5.8, §7.1).

Thin — integration-tested against a running folder_actions (:8099), not unit tests.
Each created object is marked ``managed_by`` (§14a). The unit tests use the fakes in
``tests/fakes.py`` via dependency injection (``providers``)."""
from __future__ import annotations

from typing import Optional

# resource type -> folder_actions collection path
_RESOURCE_PATH = {
    "classifier_set": "/classifier-sets",
    "notify_template": "/notify-templates",
}


class _FA:
    def __init__(self, config, bearer: Optional[str] = None):
        self.base = config.folder_actions_url.rstrip("/")
        self.timeout = config.folder_actions_timeout_s
        self._bearer = bearer

    def _client(self):
        import httpx  # lazy
        headers = {"authorization": f"Bearer {self._bearer}"} if self._bearer else {}
        return httpx.Client(base_url=self.base, timeout=self.timeout, headers=headers)


class FolderActionsResources(_FA):
    """Create/reconcile a tenant-scoped resource (classifier set / notify template)."""

    def apply(self, *, tenant, namespace, rtype, name, body, managed_by) -> str:
        path = _RESOURCE_PATH.get(rtype)
        if path is None:
            raise ValueError(f"no resource handler for type {rtype!r}")
        payload = {"name": f"{namespace}/{name}", "managed_by": managed_by, **(body or {})}
        with self._client() as c:
            r = c.post(path, json=payload, headers={"x-tenant": tenant})
            r.raise_for_status()
            data = r.json()
        return str(data.get("id") or data.get("name") or f"{rtype}:{namespace}/{name}")


class FolderActionsActions(_FA):
    """Create/reconcile a folder_actions binding on a folder."""

    def apply(self, *, folder_uid, ref, atype, on_events, mime_types, config, managed_by) -> str:
        payload = {
            "folder_uid": folder_uid, "action_type": atype, "on_events": on_events,
            "mime_types": mime_types, "config": config, "managed_by": managed_by,
        }
        with self._client() as c:
            r = c.post("/actions", json=payload)
            r.raise_for_status()
            data = r.json()
        bid = str(data.get("id") or data.get("binding_id") or "")
        # sorter routes are a sub-resource when the config carries them
        routes = (config or {}).get("routes")
        if bid and routes:
            with self._client() as c:
                c.put(f"/actions/{bid}/routes", json={"routes": routes}).raise_for_status()
        return bid
