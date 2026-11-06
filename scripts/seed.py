"""Load a few sample devices and compose's `demo` Frigate site to play with.

uv run python scripts/seed.py

With no operators yet, creates `admin` with a random password printed ONCE
(like Frigate): no default password lives in code. Change it with
scripts/create_operator.py.
"""

import asyncio
import secrets
from datetime import UTC, datetime

from sqlalchemy import text

from vigisift.config import load_settings
from vigisift.features.auth.passwords import hash_password
from vigisift.infra.db.database import apply_schema, create_engine

# The Frigate site in compose.yml: operators open the UI through port 8971 on the host,
# and the API calls internal port 5000 on the compose network (reachable only when VigiSift
# also runs in compose, as with `make demo`).
SITES = [("demo", "Demo store", "http://127.0.0.1:8971", "http://frigate:5000")]

DEVICES = [
    ("lamp-1", "Ceiling light", "living room", "light", "ONLINE"),
    ("fan-1", "Standing fan", "bedroom", "fan", "OFF"),
    ("plug-1", "TV plug", "living room", "plug", "OFF"),
]


async def main() -> None:
    engine = create_engine(load_settings())
    await apply_schema(engine)
    async with engine.begin() as connection:
        for site_id, site_name, ui_url, api_url in SITES:
            await connection.execute(
                text(
                    "INSERT INTO sites (id, name, ui_url, api_url) VALUES (:id, :name, :ui, :api)"
                    " ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name,"
                    " ui_url = EXCLUDED.ui_url, api_url = EXCLUDED.api_url"
                ),
                {"id": site_id, "name": site_name, "ui": ui_url, "api": api_url},
            )
        for device_id, name, room, kind, state in DEVICES:
            await connection.execute(
                text(
                    "INSERT INTO devices (id, name, room, kind, state, updated_at)"
                    " VALUES (:id, :name, :room, :kind, :state, :now)"
                    " ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, room = EXCLUDED.room,"
                    " state = EXCLUDED.state"
                ),
                {
                    "id": device_id,
                    "name": name,
                    "room": room,
                    "kind": kind,
                    "state": state,
                    "now": datetime.now(UTC),
                },
            )
        has_operator = (await connection.execute(text("SELECT 1 FROM operators LIMIT 1"))).first()
        password = None if has_operator else secrets.token_urlsafe(12)
        if password is not None:
            await connection.execute(
                text(
                    "INSERT INTO operators (username, display_name, password_hash, role)"
                    " VALUES ('admin', 'Admin', :hash, 'admin')"
                ),
                {"hash": hash_password(password)},
            )
    await engine.dispose()
    print(f"loaded {len(DEVICES)} devices and {len(SITES)} sites")
    if password is not None:
        print(f"created operator admin, password (printed only now): {password}")


if __name__ == "__main__":
    asyncio.run(main())
