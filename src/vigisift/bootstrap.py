"""Composition root: read Settings, build the real adapters, wire them to the ports.

The API and the MCP server both build adapters here, so the two entry points can't
drift apart in configuration. Features don't import this file; only app.py and mcp_server.py do.
"""

from functools import partial

import anthropic
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from vigisift.config import Settings
from vigisift.features.telemetry.hub import TelemetryHub
from vigisift.infra.cache.budget import RedisTokenBudget
from vigisift.infra.cache.confirmations import RedisConfirmations
from vigisift.infra.cache.cooldown import RedisCooldown
from vigisift.infra.cache.login_attempts import RedisLoginAttempts
from vigisift.infra.cache.presence import RedisPresence
from vigisift.infra.cache.sessions import RedisSessions
from vigisift.infra.db.database import create_session_factory, session_scope
from vigisift.infra.frigate.client import HttpFrigateApi
from vigisift.infra.llm.anthropic_chat import AnthropicChatModel
from vigisift.infra.llm.ollama_chat import OllamaChatModel
from vigisift.infra.llm.ollama_picker import OllamaIndicatorPicker
from vigisift.infra.mqtt.client import PahoMqttClient
from vigisift.ports.cache import EventBus
from vigisift.ports.frigate import FrigateApi
from vigisift.ports.llm import ChatModel
from vigisift.ports.messaging import MqttClient
from vigisift.ports.rule_check import IndicatorPicker
from vigisift.runtime import Runtime, SessionScope


def build_redis(settings: Settings) -> Redis:
    return Redis.from_url(settings.redis_url, decode_responses=True)


def build_sessions(engine: AsyncEngine) -> SessionScope:
    return partial(session_scope, create_session_factory(engine))


def build_frigate(settings: Settings) -> FrigateApi:
    credentials = {
        site: (credential.user, credential.password.get_secret_value())
        for site, credential in settings.frigate_credentials.items()
    }
    return HttpFrigateApi(
        credentials, settings.frigate_timeout_sec, ca_file=settings.frigate_ca_file
    )


def build_mqtt(settings: Settings) -> MqttClient:
    return PahoMqttClient(
        settings.mqtt_host,
        settings.mqtt_port,
        settings.mqtt_username,
        settings.mqtt_password,
        tls=settings.mqtt_tls,
        ca_file=settings.mqtt_ca_file,
    )


def build_rule_picker(settings: Settings) -> IndicatorPicker | None:
    if not settings.rule_check_enabled:
        return None
    return OllamaIndicatorPicker(
        settings.rule_check_model,
        settings.ollama_host,
        num_ctx=settings.rule_check_num_ctx,
        keep_alive=settings.rule_check_keep_alive,
        timeout_sec=settings.rule_check_timeout_sec,
    )


def build_chat_model(settings: Settings) -> ChatModel | None:
    if not settings.assistant_enabled:
        return None
    if settings.assistant_provider == "ollama":
        return OllamaChatModel(settings.ollama_model, settings.ollama_host)
    # The agent streams text to the client as it arrives, so with_retry can't wrap
    # a whole turn (half a sentence already sent can't be "retried"). Let the SDK
    # retry at connection open, before any byte comes back.
    return AnthropicChatModel(anthropic.AsyncAnthropic(max_retries=2), settings.assistant_model)


def build_runtime(
    settings: Settings,
    engine: AsyncEngine,
    redis: Redis,
    hub: TelemetryHub,
    events: EventBus,
    *,
    mqtt: MqttClient | None = None,
    frigate: FrigateApi | None = None,
    chat_model: ChatModel | None = None,
    rule_picker: IndicatorPicker | None = None,
) -> Runtime:
    """Adapters passed in (fakes in tests) win over adapters built from Settings."""
    return Runtime(
        settings=settings,
        session=build_sessions(engine),
        mqtt=mqtt or build_mqtt(settings),
        hub=hub,
        events=events,
        presence=RedisPresence(redis, settings.presence_ttl_sec),
        cooldown=RedisCooldown(redis),
        budget=RedisTokenBudget(redis, settings.llm_daily_token_budget),
        confirmations=RedisConfirmations(redis),
        sessions=RedisSessions(redis, settings.session_ttl_sec),
        login_attempts=RedisLoginAttempts(redis),
        frigate=frigate or build_frigate(settings),
        chat_model=chat_model or build_chat_model(settings),
        rule_picker=rule_picker or build_rule_picker(settings),
    )
