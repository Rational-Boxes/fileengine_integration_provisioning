# Copyright (C) 2026 James Hickman
# AGPL-3.0-or-later — see package header.
"""Client-IP derivation + allow-list (SPEC §3.3)."""
from provisioning_service import netutil as n


# --- ip_in ------------------------------------------------------------------

def test_ip_in_exact_and_cidr():
    assert n.ip_in("10.0.0.5", ["10.0.0.5"])
    assert n.ip_in("10.0.0.5", ["10.0.0.0/24"])
    assert not n.ip_in("10.0.1.5", ["10.0.0.0/24"])
    assert n.ip_in("::1", ["::1"])
    assert not n.ip_in("garbage", ["10.0.0.0/24"])
    assert not n.ip_in("10.0.0.5", ["not-a-net", ""])


# --- derive_client_ip -------------------------------------------------------

def test_no_xff_returns_remote():
    assert n.derive_client_ip("203.0.113.9", None, ["127.0.0.1"]) == "203.0.113.9"


def test_untrusted_peer_ignores_xff():
    # peer is not a trusted proxy → never trust XFF (spoofable)
    assert n.derive_client_ip("203.0.113.9", "1.2.3.4", ["127.0.0.1"]) == "203.0.113.9"


def test_trusted_proxy_uses_rightmost_untrusted_hop():
    # client, then two proxies; nearest peer is trusted
    ip = n.derive_client_ip(
        "127.0.0.1", "198.51.100.7, 10.0.0.2, 10.0.0.3",
        trusted_proxies=["127.0.0.1", "10.0.0.0/24"])
    assert ip == "198.51.100.7"


def test_all_hops_trusted_falls_back_to_leftmost():
    ip = n.derive_client_ip(
        "127.0.0.1", "10.0.0.9, 10.0.0.2", trusted_proxies=["127.0.0.0/8", "10.0.0.0/24"])
    assert ip == "10.0.0.9"


# --- ip_allowed -------------------------------------------------------------

def test_empty_allowlist_disables_gate():
    assert n.ip_allowed("8.8.8.8", []) is True
    assert n.ip_allowed("8.8.8.8", ["", "  "]) is True


def test_allowlist_enforced_when_set():
    assert n.ip_allowed("198.51.100.7", ["198.51.100.0/24"]) is True
    assert n.ip_allowed("203.0.113.9", ["198.51.100.0/24"]) is False
