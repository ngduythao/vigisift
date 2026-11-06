import asyncio
from collections.abc import Iterator

import psycopg
import pytest

from vigisift.config import Settings
from vigisift.infra.db.database import apply_schema, create_engine

ADMIN_URL = "postgresql://postgres:postgres@127.0.0.1:5434/postgres"
FRESH = "vigisift_test_fresh"


@pytest.fixture
def fresh_database() -> Iterator[str]:
    """An empty database: the race only shows when the tables don't exist yet."""
    with psycopg.connect(ADMIN_URL, autocommit=True) as connection:
        connection.execute(f'DROP DATABASE IF EXISTS "{FRESH}"')
        connection.execute(f'CREATE DATABASE "{FRESH}"')
    yield f"postgresql+asyncpg://postgres:postgres@127.0.0.1:5434/{FRESH}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as connection:
        connection.execute(f'DROP DATABASE IF EXISTS "{FRESH}" WITH (FORCE)')


async def test_instances_starting_together_on_an_empty_database(fresh_database: str) -> None:
    settings = Settings(_env_file=None, database_url=fresh_database)
    engines = [create_engine(settings) for _ in range(4)]
    try:
        await asyncio.gather(*(apply_schema(engine) for engine in engines))
    finally:
        for engine in engines:
            await engine.dispose()
