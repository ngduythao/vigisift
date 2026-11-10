import pytest
from pydantic import ValidationError
from sqlalchemy import text

from tests.features.alerts.frigate import message, segment
from vigisift.config import Settings
from vigisift.features.alerts import repository
from vigisift.features.alerts.payload import parse_review_message
from vigisift.features.retention import jobs
from vigisift.features.retention.job import Policy, purge
from vigisift.runtime import Runtime

POLICY = Policy(retention_days=90, reviewed_retention_days=365)


async def _alert(runtime: Runtime, review_id: str, *, age_days: float) -> None:
    async with runtime.session() as session:
        await repository.record(
            session, parse_review_message("demo", message("new", segment(review_id)))
        )
        await session.execute(
            text(
                "UPDATE camera_alerts SET started_at = now() - make_interval(days => :days)"
                " WHERE review_id = :id"
            ),
            {"days": age_days, "id": review_id},
        )


async def _judge(runtime: Runtime, review_id: str) -> None:
    async with runtime.session() as session:
        await session.execute(
            text(
                "INSERT INTO alert_verdicts (site_id, review_id, operator_id, verdict)"
                " SELECT 'demo', :id, id, 'suspicious' FROM operators WHERE username = 'anna'"
            ),
            {"id": review_id},
        )


async def _ids(runtime: Runtime, sql: str) -> set[str]:
    async with runtime.session() as session:
        return {str(row[0]) for row in await session.execute(text(sql))}


async def test_alerts_go_after_90_days_and_judged_ones_after_a_year(runtime: Runtime) -> None:
    await _alert(runtime, "1.0-recent", age_days=10)
    await _alert(runtime, "2.0-old", age_days=100)
    await _alert(runtime, "3.0-old-judged", age_days=100)
    await _alert(runtime, "4.0-ancient-judged", age_days=400)
    await _judge(runtime, "3.0-old-judged")
    await _judge(runtime, "4.0-ancient-judged")

    deleted = await purge(runtime.session, POLICY)

    assert deleted["camera_alerts"] == 2
    assert await _ids(runtime, "SELECT review_id FROM camera_alerts") == {
        "1.0-recent",
        "3.0-old-judged",
    }
    assert await _ids(runtime, "SELECT review_id FROM alert_verdicts") == {"3.0-old-judged"}, (
        "a verdict goes with its alert"
    )


async def test_llm_calls_telemetry_and_finished_commands_go_after_90_days(
    runtime: Runtime,
) -> None:
    async with runtime.session() as session:
        await session.execute(
            text(
                "INSERT INTO llm_calls (purpose, model, input_tokens, output_tokens, cost_usd,"
                " created_at) VALUES ('old', 'm', 1, 1, 0, now() - interval '100 days'),"
                " ('new', 'm', 1, 1, 0, now() - interval '10 days')"
            )
        )
        await session.execute(
            text(
                "INSERT INTO telemetry (device_id, payload_json, received_at) VALUES"
                " ('fan-1', '{\"state\": \"old\"}', now() - interval '100 days'),"
                " ('fan-1', '{\"state\": \"new\"}', now())"
            )
        )
        await session.execute(
            text(
                "INSERT INTO commands (id, device_id, idempotency_key, action, created_at, status)"
                " VALUES ('done', 'fan-1', 'k1', 'ON', now() - interval '100 days', 'ACKED'),"
                " ('stuck', 'fan-1', 'k2', 'ON', now() - interval '100 days', 'PENDING'),"
                " ('fresh', 'fan-1', 'k3', 'ON', now(), 'ACKED')"
            )
        )

    deleted = await purge(runtime.session, POLICY)

    assert deleted == {"camera_alerts": 0, "llm_calls": 1, "telemetry": 1, "commands": 1}
    assert await _ids(runtime, "SELECT purpose FROM llm_calls") == {"new"}
    assert await _ids(runtime, "SELECT payload_json->>'state' FROM telemetry") == {"new"}
    assert await _ids(runtime, "SELECT id FROM commands") == {"stuck", "fresh"}, (
        "a pending command is the sweep's to time out, not retention's"
    )


async def test_a_backlog_goes_in_batches(runtime: Runtime) -> None:
    for n in range(5):
        await _alert(runtime, f"{n}.0-old", age_days=100)

    deleted = await purge(runtime.session, POLICY, batch=2)

    assert deleted["camera_alerts"] == 5
    assert await _ids(runtime, "SELECT review_id FROM camera_alerts") == set()


async def test_the_job_runs_once_a_day_across_instances(runtime: Runtime) -> None:
    [job] = jobs.background_jobs(runtime)
    [other_instance] = jobs.background_jobs(runtime)
    await job.run()
    await _alert(runtime, "1.0-old", age_days=100)

    await other_instance.run()

    assert await _ids(runtime, "SELECT review_id FROM camera_alerts") == {"1.0-old"}, (
        "today's turn is taken"
    )
    assert job.first_run_after_sec > 0, "nothing to purge the moment the app starts"


def test_judged_alerts_cannot_be_kept_shorter_than_the_rest() -> None:
    with pytest.raises(ValidationError, match="REVIEWED_RETENTION_DAYS"):
        Settings(
            _env_file=None,
            database_url="postgresql+asyncpg://x",
            retention_days=90,
            reviewed_retention_days=30,
        )
