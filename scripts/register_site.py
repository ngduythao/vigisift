"""Register (or update) a site running Frigate.

uv run python scripts/register_site.py demo "Demo store" \\
    --ui-url http://127.0.0.1:8971 --api-url http://frigate:5000

Running again with the same id updates the name and addresses. Two steps remain outside
the DB, printed at the end: an MQTT user for the site's Frigate and its `mqtt.topic_prefix`.
"""

import argparse
import asyncio
import sys
from urllib.parse import urlsplit

from vigisift.config import load_settings
from vigisift.domain.alert import SITE_ID
from vigisift.features.alerts.repository import upsert_site
from vigisift.features.topics import frigate_topic_prefix
from vigisift.infra.db.database import apply_schema, create_engine


def _url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise argparse.ArgumentTypeError(f"needs a full http(s) URL: {value!r}")
    return value.rstrip("/")


def _site_id(value: str) -> str:
    if not SITE_ID.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "id may use only lowercase letters, digits and '-', up to 63 characters"
        )
    return value


parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
parser.add_argument("site_id", type=_site_id)
parser.add_argument("name")
parser.add_argument("--ui-url", type=_url, required=True, help="address operators open Frigate at")
parser.add_argument(
    "--api-url", type=_url, required=True, help="address VigiSift calls the Frigate API at"
)


async def main(args: argparse.Namespace) -> None:
    engine = create_engine(load_settings())
    await apply_schema(engine)
    async with engine.begin() as connection:
        await upsert_site(connection, args.site_id, args.name, args.ui_url, args.api_url)
    await engine.dispose()
    user = f"frigate-{args.site_id}"
    print(f"registered site {args.site_id}")
    print("The site's Frigate config:")
    print(f"  mqtt.topic_prefix: {frigate_topic_prefix(args.site_id)}")
    print(f"  mqtt.client_id: {user}")
    print(f"  mqtt.user: {user}")
    print("Mosquitto (deploy/mosquitto/acl):")
    print(f"  user {user}")
    print(f"  topic readwrite {frigate_topic_prefix(args.site_id)}/#")
    print(f"and create the password: mosquitto_passwd /mosquitto/config/passwd {user}")


if __name__ == "__main__":
    asyncio.run(main(parser.parse_args(sys.argv[1:])))
