from redis.asyncio import Redis

from vigisift.infra.cache.budget import RedisTokenBudget


async def test_spending_lowers_what_remains_today(redis: Redis) -> None:
    budget = RedisTokenBudget(redis, daily_limit=1000)

    await budget.spend(300)
    await budget.spend(200)

    assert await budget.remaining() == 500


async def test_remaining_never_goes_negative(redis: Redis) -> None:
    budget = RedisTokenBudget(redis, daily_limit=100)

    await budget.spend(250)

    assert await budget.remaining() == 0
