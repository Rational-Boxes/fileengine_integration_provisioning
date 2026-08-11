# Copyright (C) 2026 James Hickman
# AGPL-3.0-or-later — see package header.
"""Client-IP derivation + allow-list membership (SPEC §3.3).

Pure functions so the source-IP defense-in-depth gate is unit-testable. Behind the
edge (nginx) the real client IP comes from ``X-Forwarded-For``; we only trust XFF when
the immediate peer is a configured trusted proxy, and we walk right→left skipping
trusted hops to find the first untrusted address (the real client)."""
from __future__ import annotations

import ipaddress
from typing import Iterable, Optional


def _parse_ip(s: str) -> Optional[ipaddress._BaseAddress]:
    try:
        return ipaddress.ip_address(s.strip())
    except ValueError:
        return None


def ip_in(ip: str, nets: Iterable[str]) -> bool:
    """True if ``ip`` is (equal to / within) any entry in ``nets`` (plain IPs or
    CIDRs, IPv4 or IPv6). Invalid entries are ignored."""
    addr = _parse_ip(ip)
    if addr is None:
        return False
    for n in nets:
        n = n.strip()
        if not n:
            continue
        try:
            if "/" in n:
                if addr in ipaddress.ip_network(n, strict=False):
                    return True
            else:
                other = _parse_ip(n)
                if other is not None and addr == other:
                    return True
        except ValueError:
            continue
    return False


def derive_client_ip(remote_addr: str, xff: Optional[str],
                     trusted_proxies: Iterable[str]) -> str:
    """The real client IP. If the immediate peer (``remote_addr``) is a trusted
    proxy and an ``X-Forwarded-For`` header is present, return the right-most hop
    that is *not* a trusted proxy; otherwise return ``remote_addr`` verbatim (never
    trust XFF from an untrusted peer)."""
    trusted = list(trusted_proxies)
    if not xff or not ip_in(remote_addr, trusted):
        return remote_addr
    hops = [h.strip() for h in xff.split(",") if h.strip()]
    for ip in reversed(hops):
        if not ip_in(ip, trusted):
            return ip
    # every hop was a trusted proxy — fall back to the left-most (original) hop
    return hops[0] if hops else remote_addr


def ip_allowed(ip: str, allowlist: Iterable[str]) -> bool:
    """Allow-list membership. An **empty** allow-list means the gate is *disabled*
    (opt-in per integration, §14.2a): returns True. Otherwise the ip must match."""
    allow = [a for a in allowlist if a and a.strip()]
    if not allow:
        return True
    return ip_in(ip, allow)
