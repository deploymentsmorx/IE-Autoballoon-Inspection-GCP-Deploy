"""Auth / org / engineer store facade (Postgres + JSON fallback)."""

from __future__ import annotations

# Re-export existing store implementation during migration
from app import store as _store

startup = _store.startup
find_user_by_login = _store.find_user_by_login
get_user = _store.get_user
list_orgs = _store.list_orgs
create_org = _store.create_org
list_engineers = _store.list_engineers
add_engineer = _store.add_engineer
update_access = _store.update_access
grant_full = _store.grant_full
change_password = _store.change_password
delete_engineer = _store.delete_engineer
