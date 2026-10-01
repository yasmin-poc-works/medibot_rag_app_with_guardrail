"""Role-based document access rules applied before retrieval."""

from dataclasses import dataclass
from secrets import token_urlsafe

from .schemas import Role

ROLE_COLLECTIONS: dict[Role, tuple[str, ...]] = {
    "doctor": ("clinical", "general"),
    "nurse": ("nursing", "clinical", "general"),
    "billing_executive": ("billing", "general"),
    "technician": ("equipment", "general"),
    "admin": ("clinical", "nursing", "billing", "equipment", "general"),
}

DEMO_USERS = {
    "dr.mehta": ("doctor", "doctor123"),
    "nurse.priya": ("nurse", "nurse123"),
    "billing.ravi": ("billing_executive", "billing123"),
    "tech.anand": ("technician", "tech123"),
    "admin.sys": ("admin", "admin123"),
}

# This is intentionally an in-memory session store for the assignment demo.
# Restarting the backend invalidates all demo sessions.
ACTIVE_SESSIONS: dict[str, "AuthenticatedUser"] = {}


@dataclass(frozen=True)
class AuthenticatedUser:
    """Small immutable identity object used by the service layer."""

    username: str
    role: Role


def authenticate(username: str, password: str) -> AuthenticatedUser | None:
    """Authenticate an assignment demo account without external identity services."""

    account = DEMO_USERS.get(username)
    if account is None or account[1] != password:
        return None
    return AuthenticatedUser(username=username, role=account[0])


def create_session(user: AuthenticatedUser) -> str:
    """Create an opaque demo session token for an authenticated user."""

    token = token_urlsafe(32)
    ACTIVE_SESSIONS[token] = user
    return token


def get_session(token: str) -> AuthenticatedUser | None:
    """Return the user for an active token, if one exists."""

    return ACTIVE_SESSIONS.get(token)


def revoke_session(token: str) -> bool:
    """Revoke a session token and report whether it was active."""

    return ACTIVE_SESSIONS.pop(token, None) is not None


def allowed_collections(role: Role) -> list[str]:
    """Return the collections that may be used in a retrieval filter."""

    return list(ROLE_COLLECTIONS[role])
