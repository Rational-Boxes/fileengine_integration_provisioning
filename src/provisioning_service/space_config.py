# Copyright (C) 2026 James Hickman
# AGPL-3.0-or-later — see package header.
"""Per-space configuration & setup API helpers (SPEC §6.3).

Pure functions assembling a space's resolved automation config (each blueprint action's
ref/folder/binding + current param values with ``secret`` params redacted) and merging a
per-space param patch. The endpoints in :mod:`api` call these; the re-render of patched
bindings is a reconcile through the existing :class:`~provisioning_service.engine.Engine`.
"""

from __future__ import annotations

REDACTED = "***"


def secret_param_names(blueprint: dict) -> set:
    """Names of params declared ``type: secret`` in the blueprint schema (§5.4)."""
    params = (blueprint or {}).get("params") or {}
    if not isinstance(params, dict):
        return set()
    return {name for name, spec in params.items()
            if isinstance(spec, dict) and spec.get("type") == "secret"}


def redact_params(params: dict, blueprint: dict) -> dict:
    """Copy of ``params`` with every ``secret``-typed value replaced by ``***``."""
    secrets = secret_param_names(blueprint)
    return {k: (REDACTED if k in secrets else v) for k, v in (params or {}).items()}


def merge_params(current: dict, patch: dict) -> dict:
    """Shallow-merge a per-space param patch over the current values (§6.3 PATCH)."""
    merged = dict(current or {})
    merged.update(patch or {})
    return merged


def resolved_config(space: dict, blueprint: dict) -> dict:
    """The space's resolved automation config (§6.3 GET): each blueprint action joined
    to its persisted binding, plus the current param values with secrets redacted."""
    bindings = {b.get("ref"): b for b in (space.get("bindings") or [])}
    actions = []
    for a in (blueprint or {}).get("actions") or []:
        ref = a.get("ref")
        b = bindings.get(ref, {})
        actions.append({
            "ref": ref,
            "type": a.get("type"),
            "folder": a.get("folder"),
            "folder_uid": b.get("folder_uid"),
            "binding_id": b.get("binding_id"),
            "on_events": a.get("on_events") or [],
            "mime_types": a.get("mime_types") or [],
        })
    return {
        "space_uid": space.get("space_uid"),
        "external_id": space.get("external_id"),
        "version": space.get("version"),
        "params": redact_params(space.get("params"), blueprint),
        "actions": actions,
    }
