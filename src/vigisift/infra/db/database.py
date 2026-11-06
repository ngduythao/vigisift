import pathlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from vigisift.config import Settings

_SCHEMA = pathlib.Path(__file__).with_name("schema.sql")


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(settings.database_url, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


def _statements(sql: str) -> list[str]:
    """Strip comments first, split statements after.

    The other way round, a ; inside a comment cuts a statement in two, and Postgres
    reports "syntax error at end of input" somewhere nobody expects.
    """
    code = "\n".join(
        line for line in sql.splitlines() if line.strip() and not line.strip().startswith("--")
    )
    return [chunk for chunk in code.split(";") if chunk.strip()]


async def apply_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        # Instances starting together would race on `CREATE ... IF NOT EXISTS` and
        # `CREATE OR REPLACE VIEW`; one applies the schema at a time instead.
        await connection.execute(text("SELECT pg_advisory_xact_lock(hashtext('vigisift.schema'))"))
        for statement in _statements(_SCHEMA.read_text()):
            await connection.execute(text(statement))


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    session = factory()
    try:
        yield session
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    finally:
        await session.close()
