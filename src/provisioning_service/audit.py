# Copyright (C) 2026 James Hickman
# AGPL-3.0-or-later — see package header.
"""Audit emission (SPEC §8) to the platform Redis audit stream (drained by
audit_service). Behind an ``Audit`` protocol so the router is testable against a fake.
Best-effort: a failed emit is logged and swallowed, never failing a request."""
from __future__ import annotations

import json
import logging
from typing import Protocol, runtime_checkable

log = logging.getLogger("provisioning_service.audit")

# Event names (SPEC §8).
SPACE_APPLIED = "provisioning.space_applied"
SPACE_RECONCILED = "provisioning.space_reconciled"
SPACE_DELETED = "provisioning.space_deleted"
ACTION_CONFIGURED = "provisioning.action_configured"
RESOURCE_APPLIED = "provisioning.resource_applied"
REJECTED = "provisioning.rejected"


@runtime_checkable
class Audit(Protocol):
    def emit(self, event: str, **fields) -> None: ...


class NullAudit:
    def emit(self, event: str, **fields) -> None:  # no-op
        return None


class RedisAudit:
    """XADD ``{event, payload}`` onto the shared audit stream. Best-effort."""

    def __init__(self, config):
        self.config = config
        self._r = None

    def _redis(self):
        import redis  # lazy
        if self._r is None:
            c = self.config
            self._r = redis.Redis(host=c.redis_host, port=c.redis_port,
                                  password=c.redis_password or None, db=c.redis_db)
        return self._r

    def emit(self, event: str, **fields) -> None:
        payload = {"event": event, "source": "provisioning", **fields}
        try:
            self._redis().xadd(
                self.config.audit_stream,
                {"payload": json.dumps(payload)}, maxlen=100000, approximate=True)
        except Exception:
            log.warning("audit emit failed (%s) — continuing", event, exc_info=True)


def space_event(status: str) -> str:
    """Map an apply status to its space event (created/reconciled)."""
    return SPACE_APPLIED if status == "created" else SPACE_RECONCILED
