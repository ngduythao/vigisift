import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from vigisift import background, bootstrap
from vigisift.config import Settings, load_settings
from vigisift.features import topics
from vigisift.features.alerts import jobs as alert_jobs
from vigisift.features.alerts.handlers import on_review
from vigisift.features.alerts.routes import router as alerts_router
from vigisift.features.assistant.routes import router as assistant_router
from vigisift.features.auth.guard import require_operator
from vigisift.features.auth.origin import same_origin
from vigisift.features.auth.routes import router as auth_router
from vigisift.features.costs.routes import router as costs_router
from vigisift.features.devices import jobs as device_jobs
from vigisift.features.devices.handlers import on_ack, on_status
from vigisift.features.devices.routes import router as devices_router
from vigisift.features.retention import jobs as retention_jobs
from vigisift.features.review.routes import router as review_router
from vigisift.features.rules import jobs as rule_jobs
from vigisift.features.rules.routes import router as rules_router
from vigisift.features.telemetry.handlers import on_telemetry
from vigisift.features.telemetry.hub import TelemetryHub
from vigisift.features.telemetry.routes import router as telemetry_router
from vigisift.infra.cache.events import RedisEventBus
from vigisift.infra.db.database import apply_schema, create_engine
from vigisift.ports.frigate import FrigateApi
from vigisift.ports.llm import ChatModel
from vigisift.ports.messaging import MqttClient
from vigisift.ports.rule_check import IndicatorPicker
from vigisift.runtime import Runtime

# API instances in the same group share messages instead of each handling its
# own copy. STATUS is the exception: it is retained, the broker doesn't send
# retained messages to shared subscriptions, and every instance needs to know
# who is online as soon as it starts.
SHARED_GROUP = "vigisift"

WEB_DIR = Path(__file__).with_name("web")
INDEX_HTML = WEB_DIR / "index.html"


def subscribe(runtime: Runtime) -> None:
    runtime.mqtt.subscribe(topics.TELEMETRY, partial(on_telemetry, runtime), SHARED_GROUP)
    runtime.mqtt.subscribe(topics.ACK, partial(on_ack, runtime), SHARED_GROUP)
    runtime.mqtt.subscribe(topics.FRIGATE_REVIEWS, partial(on_review, runtime), SHARED_GROUP)
    runtime.mqtt.subscribe(topics.STATUS, partial(on_status, runtime))


def background_jobs(runtime: Runtime) -> list[background.Job]:
    return [
        *alert_jobs.background_jobs(runtime),
        *rule_jobs.background_jobs(runtime),
        *device_jobs.background_jobs(runtime),
        *retention_jobs.background_jobs(runtime),
    ]


def create_app(
    settings: Settings | None = None,
    mqtt: MqttClient | None = None,
    chat_model: ChatModel | None = None,
    frigate: FrigateApi | None = None,
    rule_picker: IndicatorPicker | None = None,
) -> FastAPI:
    resolved = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(resolved)
        await apply_schema(engine)
        redis = bootstrap.build_redis(resolved)
        hub = TelemetryHub()
        events = RedisEventBus(redis, hub.publish, resolved.events_channel)
        listener = asyncio.create_task(events.run())
        await events.wait_ready()
        runtime = bootstrap.build_runtime(
            resolved,
            engine,
            redis,
            hub,
            events,
            mqtt=mqtt,
            frigate=frigate,
            chat_model=chat_model,
            rule_picker=rule_picker,
        )
        app.state.runtime = runtime
        subscribe(runtime)
        jobs = background.start(background_jobs(runtime))
        try:
            yield
        finally:
            await background.stop([*jobs, listener])
            runtime.mqtt.close()
            await runtime.frigate.aclose()
            if runtime.rule_picker is not None:
                await runtime.rule_picker.aclose()
            await redis.aclose()
            await engine.dispose()

    app = FastAPI(title="VigiSift", lifespan=lifespan)
    # Sign-in is the only open door; every other route needs a signed-in operator.
    # The WebSocket checks inside its route (a dependency can't return 401 there).
    app.include_router(auth_router)
    signed_in = [Depends(require_operator)]
    app.include_router(devices_router, dependencies=signed_in)
    app.include_router(telemetry_router)
    app.include_router(alerts_router, dependencies=signed_in)
    app.include_router(review_router, dependencies=signed_in)
    app.include_router(rules_router, dependencies=signed_in)
    app.include_router(costs_router, dependencies=signed_in)
    app.include_router(assistant_router, dependencies=signed_in)

    @app.middleware("http")
    async def same_origin_writes(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Block writes started from another page (CSRF), a second layer after SameSite cookies."""
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not same_origin(
            request.headers.get("origin"),
            request.headers.get("sec-fetch-site"),
            request.headers.get("host"),
            resolved.public_origin,
        ):
            return JSONResponse({"detail": "cross_origin_request"}, status_code=403)
        return await call_next(request)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        # no-cache: the browser always asks again (cheap, with an ETag), so after each
        # deploy no operator stays on the old UI.
        return FileResponse(INDEX_HTML, headers={"Cache-Control": "no-cache"})

    # Public because the sign-in page needs them too. Bundled, not loaded from a
    # CDN, so sites with restricted internet get them and nothing calls out.
    app.mount("/fonts", StaticFiles(directory=WEB_DIR / "fonts"), name="fonts")

    return app
