from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from vigisift.domain.operator import Operator, Role


def _operator(row: Any) -> Operator:
    return Operator(row.id, row.username, row.display_name, Role(row.role))


async def by_id(session: AsyncSession, operator_id: int) -> tuple[Operator, int] | None:
    """An active operator and their current password generation.

    Returns only unlocked accounts: locking takes effect on the next request.
    """
    row = (
        await session.execute(
            text(
                "SELECT id, username, display_name, role, session_epoch FROM operators"
                " WHERE id = :id AND NOT disabled"
            ),
            {"id": operator_id},
        )
    ).one_or_none()
    return (_operator(row), row.session_epoch) if row is not None else None


async def credentials(session: AsyncSession, username: str) -> tuple[Operator, str, int] | None:
    row = (
        await session.execute(
            text(
                "SELECT id, username, display_name, role, password_hash, session_epoch"
                " FROM operators WHERE username = :username AND NOT disabled"
            ),
            {"username": username},
        )
    ).one_or_none()
    return (_operator(row), row.password_hash, row.session_epoch) if row is not None else None


async def upsert(
    session: AsyncSession, username: str, display_name: str, password_hash: str, role: Role
) -> int:
    """Create an account or reset it. A reset ends all of their open sessions."""
    row = await session.execute(
        text(
            "INSERT INTO operators (username, display_name, password_hash, role)"
            " VALUES (:username, :name, :hash, :role)"
            " ON CONFLICT (username) DO UPDATE SET display_name = EXCLUDED.display_name,"
            " password_hash = EXCLUDED.password_hash, role = EXCLUDED.role, disabled = FALSE,"
            " session_epoch = operators.session_epoch + 1"
            " RETURNING id"
        ),
        {"username": username, "name": display_name, "hash": password_hash, "role": role.value},
    )
    operator_id: int = row.scalar_one()
    return operator_id
