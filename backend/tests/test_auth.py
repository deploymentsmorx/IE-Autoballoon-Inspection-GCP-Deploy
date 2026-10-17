from __future__ import annotations

from app.core.security import create_token, hash_password, verify_password
from app.services import auth_service


def test_password_roundtrip():
    h = hash_password("admin123")
    assert verify_password("admin123", h)
    assert not verify_password("wrong", h)


def test_find_admin_seed():
    # Requires Postgres or local JSON seed from startup
    mode = auth_service.startup(hash_password)
    assert mode in {"postgres", "json"}
    user = auth_service.find_user_by_login("admin@smorx.ai")
    assert user is not None
    assert user["role"] == "super_admin"
    token = create_token(user)
    assert isinstance(token, str) and len(token) > 20
