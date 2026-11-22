"""Who is calling, as seen by the routes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from fastapi import HTTPException, Request, status


@dataclass(frozen=True)
class Principal:
    """``api_key`` callers are trusted backends: they may act for any ``user_id``.

    ``jwt`` callers are end users: ``subject`` is their user ID and they may act only for
    themselves, unless the token carries the admin scope (``auth.jwt.admin_scope``).
    """

    method: Literal["none", "api_key", "jwt"]
    subject: str | None = None
    scopes: frozenset[str] = field(default_factory=frozenset)
    admin: bool = False

    @property
    def is_end_user(self) -> bool:
        return self.method == "jwt" and not self.admin

    def resolve_user_id(self, requested: str | None) -> str | None:
        """The user ID a request should run as. End users can't name someone else."""
        if not self.is_end_user:
            return requested
        if requested is not None and requested != self.subject:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "user_id does not match the token")
        return self.subject


ANONYMOUS = Principal(method="none", admin=True)


def current_principal(request: Request) -> Principal:
    return getattr(request.state, "principal", ANONYMOUS)


def require_admin(request: Request) -> Principal:
    """For routes end users must not reach (ingestion, documents, jobs)."""
    principal = current_principal(request)
    if principal.is_end_user:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "this route needs an API key or admin scope")
    return principal
