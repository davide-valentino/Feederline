"""P0 operator boundary (scope decision D4).

A single static token from settings guards every ``/internal/*`` route. This is a
boundary, not an identity system: the caller's actor label is fixed in configuration
until P1 introduces real RBAC.
"""

import hmac

from fastapi import Header, HTTPException, status

from app.config import settings


def _extract_token(
    authorization: str | None, operator_token: str | None
) -> str | None:
    if operator_token:
        return operator_token.strip()
    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value:
            return value.strip()
    return None


def require_operator(
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_operator_token: str | None = Header(default=None, alias="X-Operator-Token"),
) -> str:
    """Return the operator actor label or raise 401/503.

    Both ``Authorization: Bearer <token>`` and ``X-Operator-Token`` are accepted so the
    placeholder is easy to use from curl and from a future portal. When no token is
    configured the boundary fails closed with 503 rather than accepting anything.
    """

    presented = _extract_token(authorization, x_operator_token)
    expected = settings.operator_token
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Operator boundary is not configured",
        )
    if presented is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Operator token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not hmac.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid operator token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return settings.operator_actor
