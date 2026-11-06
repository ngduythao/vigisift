"""Create an operator account (or reset its password). There is no default account.

Resetting a password signs out every open session of that account.

uv run python scripts/create_operator.py sam "Sam" --admin
docker compose exec api python scripts/create_operator.py sam "Sam" --admin

The password is asked at the keyboard (hidden, stays out of shell history). When running
non-interactively, set VIGISIFT_OPERATOR_PASSWORD.
"""

import argparse
import asyncio
import getpass
import os
import sys

from vigisift.config import load_settings
from vigisift.domain.operator import MIN_PASSWORD_LENGTH, USERNAME, Role
from vigisift.features.auth import repository
from vigisift.features.auth.passwords import hash_password
from vigisift.infra.db.database import (
    apply_schema,
    create_engine,
    create_session_factory,
    session_scope,
)


def _username(value: str) -> str:
    if not USERNAME.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "3 to 32 characters: lowercase letters, digits, '.', '_', '-'"
        )
    return value


def _password() -> str:
    password = os.environ.get("VIGISIFT_OPERATOR_PASSWORD")
    if password is None:
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Repeat: "):
            sys.exit("the two entries don't match")
    if len(password) < MIN_PASSWORD_LENGTH:
        sys.exit(f"password needs at least {MIN_PASSWORD_LENGTH} characters")
    return password


parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
parser.add_argument("username", type=_username)
parser.add_argument("display_name")
parser.add_argument("--admin", action="store_true", help="may edit camera rules and export labels")


async def main(args: argparse.Namespace) -> None:
    password_hash = hash_password(_password())
    engine = create_engine(load_settings())
    await apply_schema(engine)
    async with session_scope(create_session_factory(engine)) as session:
        await repository.upsert(
            session,
            args.username,
            args.display_name,
            password_hash,
            Role.ADMIN if args.admin else Role.OPERATOR,
        )
    await engine.dispose()
    print(f"created/updated operator {args.username}")


if __name__ == "__main__":
    asyncio.run(main(parser.parse_args(sys.argv[1:])))
