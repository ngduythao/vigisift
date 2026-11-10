from vigisift.config import Settings


def test_an_empty_value_from_compose_counts_as_unset() -> None:
    """compose passes `${PUBLIC_ORIGIN:-}` as "" when unset. Taken literally, "" as the public
    origin would refuse every write and WebSocket."""
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://x",
        public_origin="",
        mqtt_ca_file="",
        frigate_ca_file="",
    )

    assert settings.public_origin is None
    assert settings.mqtt_ca_file is None
    assert settings.frigate_ca_file is None
