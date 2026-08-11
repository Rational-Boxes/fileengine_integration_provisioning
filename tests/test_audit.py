# Copyright (C) 2026 James Hickman
# AGPL-3.0-or-later — see package header.
"""Audit protocol + helpers (SPEC §8)."""
from provisioning_service import audit as au

from fakes import FakeAudit


def test_fakeaudit_satisfies_protocol():
    assert isinstance(FakeAudit(), au.Audit)
    assert isinstance(au.NullAudit(), au.Audit)


def test_space_event_maps_status():
    assert au.space_event("created") == au.SPACE_APPLIED
    assert au.space_event("reconciled") == au.SPACE_RECONCILED


def test_nullaudit_is_noop():
    au.NullAudit().emit("x", a=1)   # no raise


def test_fakeaudit_records():
    a = FakeAudit()
    a.emit(au.SPACE_APPLIED, tenant="t", outcome="created")
    assert a.names() == [au.SPACE_APPLIED]
    assert a.events[0][1]["tenant"] == "t"
