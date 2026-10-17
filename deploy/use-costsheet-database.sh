#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ADMIN_ENV="$APP_DIR/.env.smorx-inspection.production"
COSTSHEET_ENV="${COSTSHEET_ENV_FILE:-$HOME/apps/smorx-costsheet/.env.smorx-costsheet.production}"

if [[ ! -f "$ADMIN_ENV" ]]; then
  echo "Missing $ADMIN_ENV" >&2
  exit 1
fi
if [[ ! -f "$COSTSHEET_ENV" ]]; then
  echo "Missing Cost Sheet environment file: $COSTSHEET_ENV" >&2
  exit 1
fi

python3 - "$ADMIN_ENV" "$COSTSHEET_ENV" <<'PY'
from pathlib import Path
import os
import subprocess
import sys
import tempfile
from urllib.parse import quote

admin_path = Path(sys.argv[1])
costsheet_path = Path(sys.argv[2])

def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key.strip()] = value
    return values

costsheet_env = read_env(costsheet_path)
required = ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")
missing = [key for key in required if not costsheet_env.get(key)]
if missing:
    raise SystemExit("Cost Sheet env file is missing required PostgreSQL settings.")

user = costsheet_env["POSTGRES_USER"]
password = costsheet_env["POSTGRES_PASSWORD"]
database = costsheet_env["POSTGRES_DB"]

network = "smorx-costsheet_default"
subprocess.run(
    ["docker", "network", "inspect", network],
    stdout=subprocess.DEVNULL,
    check=True,
)
subprocess.run(
    [
        "docker", "exec", "smorx-costsheet-db", "psql",
        "-U", user, "-d", database, "-v", "ON_ERROR_STOP=1",
        "-c", "CREATE SCHEMA IF NOT EXISTS costsheet_admin AUTHORIZATION CURRENT_USER; "
              "GRANT ALL ON SCHEMA costsheet_admin TO CURRENT_USER;",
    ],
    check=True,
)

userinfo = f"{quote(user, safe='')}:{quote(password, safe='')}"
database_path = quote(database, safe="")
options = quote("-csearch_path=costsheet_admin", safe="")
database_url = (
    f"postgresql://{userinfo}@smorx-costsheet-db:5432/{database_path}"
    f"?options={options}"
)

lines = admin_path.read_text(encoding="utf-8").splitlines()
updated: list[str] = []
replaced = False
for line in lines:
    if line.startswith("DATABASE_URL="):
        updated.append(f"DATABASE_URL={database_url}")
        replaced = True
    else:
        updated.append(line)
if not replaced:
    updated.append(f"DATABASE_URL={database_url}")

mode = admin_path.stat().st_mode & 0o777
with tempfile.NamedTemporaryFile(
    mode="w", encoding="utf-8", newline="\n", dir=admin_path.parent,
    prefix=admin_path.name + ".", delete=False,
) as temp:
    temp.write("\n".join(updated) + "\n")
    temp_path = Path(temp.name)
os.chmod(temp_path, mode or 0o600)
os.replace(temp_path, admin_path)
os.chmod(admin_path, 0o600)
print("Admin database target set to smorx-costsheet-db, schema costsheet_admin.")
print("Database credentials were not displayed.")
PY
