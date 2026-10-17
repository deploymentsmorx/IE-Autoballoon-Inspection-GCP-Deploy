"""Local JSON fallback when Postgres is unreachable — re-export."""

from app.services.local_store import *  # noqa: F401,F403
from app.services.local_store import DB_PATH, ensure_seed, load, save  # noqa: F401
