"""Compatibility shim — prefer app.api.dependencies."""

from app.api.dependencies import current_user, require_admin, require_perm  # noqa: F401
