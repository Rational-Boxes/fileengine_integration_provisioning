# Copyright (C) 2026 James Hickman
# AGPL-3.0-or-later — see package header.
"""Local HS256 verification of the bridge/integration-service JWT (SPEC §3).

Mirrors the platform's shared-secret verify: alg pinned to HS256 (reject alg:none
and RS/HS confusion), ``exp`` enforced. A bridge-issued token is accepted across the
stack when every service shares ``FILEENGINE_JWT_SECRET`` (embedding-kit §4.2)."""
from __future__ import annotations

from typing import Any

import jwt  # PyJWT


class TokenError(ValueError):
    """Invalid/expired/untrusted token."""


def verify_bridge_token(token: str, secret: str, *, leeway: int = 0,
                        issuer: str | None = None) -> dict[str, Any]:
    """Verify + decode an HS256 bridge JWT. Raises :class:`TokenError` on any
    problem. Returns the claims dict."""
    if not secret:
        raise TokenError("no shared JWT secret configured")
    if not token:
        raise TokenError("empty token")
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],          # pinned — no none/RS confusion
            leeway=leeway,
            options={"require": ["exp"], "verify_exp": True},
            issuer=issuer,
        )
    except jwt.ExpiredSignatureError as e:
        raise TokenError("token expired") from e
    except jwt.InvalidTokenError as e:
        raise TokenError(f"invalid token: {e}") from e
    if not isinstance(claims, dict):
        raise TokenError("malformed claims")
    return claims
