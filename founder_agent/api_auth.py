"""Bearer-token authentication and role-based access control for the HTTP API.

The tokens are deployment credentials, not agent capabilities. They are loaded
from the environment, compared in constant time, and never accepted through a
caller-controlled identity header.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass
from typing import Literal, cast

from .config import Settings

ApiRole = Literal["reader", "approver", "executor", "auditor", "admin"]
MIN_TOKEN_LENGTH = 32

_ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "reader": frozenset({"read"}),
    "approver": frozenset({"read", "actions:read", "actions:approve"}),
    "executor": frozenset({"read", "actions:read", "actions:execute"}),
    "auditor": frozenset({"audit"}),
    "admin": frozenset({
        "read", "actions:read", "actions:approve", "actions:execute", "audit", "admin",
    }),
}


@dataclass(frozen=True)
class ApiIdentity:
    subject: str
    role: ApiRole


class ApiAuthenticationError(PermissionError):
    pass


class ApiAuthorizationError(PermissionError):
    pass


def authenticate(token: str | None, settings: Settings) -> ApiIdentity:
    if not token:
        raise ApiAuthenticationError("bearer credential is required")
    if len(token) < MIN_TOKEN_LENGTH:
        raise ApiAuthenticationError("invalid bearer credential")

    # Check every configured credential rather than returning on the first
    # comparison. This keeps role lookup from becoming a useful timing oracle.
    matched_role: str | None = None
    for role, configured in settings.api_tokens.items():
        if len(configured) >= MIN_TOKEN_LENGTH and hmac.compare_digest(token, configured):
            matched_role = role
    if matched_role not in _ROLE_PERMISSIONS:
        raise ApiAuthenticationError("invalid bearer credential")
    role = cast(ApiRole, matched_role)
    return ApiIdentity(subject=f"api:{role}", role=role)


def authorize(identity: ApiIdentity, permission: str) -> None:
    if permission not in _ROLE_PERMISSIONS.get(identity.role, frozenset()):
        raise ApiAuthorizationError(
            f"role '{identity.role}' is not permitted to perform '{permission}'")
