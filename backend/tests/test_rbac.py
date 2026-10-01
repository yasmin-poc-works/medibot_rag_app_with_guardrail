"""Tests for demo authentication, sessions, and role-to-collection rules."""

import pytest

from mediassist import rbac


@pytest.fixture(autouse=True)
def _clear_sessions():
    rbac.ACTIVE_SESSIONS.clear()
    yield
    rbac.ACTIVE_SESSIONS.clear()


def test_authenticate_returns_user_with_role():
    user = rbac.authenticate("tech.anand", "tech123")

    assert user == rbac.AuthenticatedUser(username="tech.anand", role="technician")


@pytest.mark.parametrize("username,password", [("tech.anand", "nope"), ("ghost", "tech123")])
def test_authenticate_rejects_bad_credentials(username, password):
    assert rbac.authenticate(username, password) is None


def test_session_lifecycle():
    user = rbac.authenticate("admin.sys", "admin123")
    token = rbac.create_session(user)

    assert rbac.get_session(token) == user
    assert rbac.revoke_session(token) is True
    assert rbac.get_session(token) is None
    assert rbac.revoke_session(token) is False


def test_session_tokens_are_unique():
    user = rbac.authenticate("dr.mehta", "doctor123")

    assert rbac.create_session(user) != rbac.create_session(user)


def test_every_role_can_read_general():
    for role in rbac.ROLE_COLLECTIONS:
        assert "general" in rbac.allowed_collections(role)


def test_admin_can_read_every_collection():
    every_collection = {name for names in rbac.ROLE_COLLECTIONS.values() for name in names}

    assert set(rbac.allowed_collections("admin")) == every_collection


@pytest.mark.parametrize(
    "role,forbidden",
    [
        ("doctor", {"billing", "equipment", "nursing"}),
        ("nurse", {"billing", "equipment"}),
        ("billing_executive", {"clinical", "nursing", "equipment"}),
        ("technician", {"clinical", "nursing", "billing"}),
    ],
)
def test_roles_are_isolated_from_restricted_collections(role, forbidden):
    assert not forbidden & set(rbac.allowed_collections(role))


def test_allowed_collections_returns_a_copy():
    collections = rbac.allowed_collections("doctor")
    collections.append("billing")

    assert "billing" not in rbac.allowed_collections("doctor")
