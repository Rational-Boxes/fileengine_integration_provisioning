# Copyright (C) 2026 James Hickman
# AGPL-3.0-or-later — see package header.
"""Core protocol conformance + FakeCore behavior (SPEC §3, §7)."""
from provisioning_service.core import Core, GrpcCore, PERM_NAME

from fakes import FakeCore


def test_fakecore_satisfies_core_protocol():
    assert isinstance(FakeCore(), Core)


def test_grpccore_satisfies_core_protocol():
    class _Cfg:
        provisioning_role = "provisioning"
        grpc_address = "localhost:50051"
    assert isinstance(GrpcCore(_Cfg(), integration_id="i", tenant="t"), Core)


def test_perm_name_map_covers_letters():
    for letter in "rwdluvbsmix":
        assert letter in PERM_NAME
    assert PERM_NAME["r"] == "READ" and PERM_NAME["m"] == "MANAGE_ACL"


def test_fakecore_make_dir_idempotent():
    c = FakeCore()
    a = c.make_dir("root", "Docs")
    b = c.make_dir("root", "Docs")   # same name → same uid
    assert a == b
    assert c.child_by_name("root", "Docs") == a
    # only one make_dir was actually recorded (second short-circuited)
    assert [x for x in c.calls if x[0] == "make_dir"] == [("make_dir", "root", "Docs", a)]


def test_fakecore_nested_and_path_resolution():
    c = FakeCore()
    inc = c.make_dir("root", "Incoming")
    rej = c.make_dir(inc, "Rejected")
    assert c.path_uid("Incoming", "Rejected") == rej
    assert c.path_uid("Nope") is None


def test_fakecore_metadata_and_acls():
    c = FakeCore()
    d = c.make_dir("root", "Approved")
    c.set_metadata(d, "managed_by", "acme-crm")
    assert c.get_metadata(d)["managed_by"] == "acme-crm"
    c.grant(d, "role:eng", "r")
    c.grant(d, "role:eng", "w")
    assert {(a["principal"], a["perm"]) for a in c.get_acls(d)} == {("role:eng", "r"), ("role:eng", "w")}
    c.revoke(d, "role:eng", "w")
    assert {(a["principal"], a["perm"]) for a in c.get_acls(d)} == {("role:eng", "r")}
