"""Compatibility shim — prefer app.api.routes.*"""

from app.api.routes import auth as auth_admin  # noqa: F401
from app.api.routes import autoballoon as jobs  # noqa: F401
