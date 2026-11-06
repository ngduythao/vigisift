.PHONY: install up down seed lint format typecheck test check run frigate-config detector demo mcp

# The host's time zone for Frigate (see compose.yml), unless TZ is already set.
TZ ?= $(shell readlink /etc/localtime 2>/dev/null | sed 's|.*/zoneinfo/||')
export TZ

install:
	uv sync

up:
	docker compose up -d --wait postgres mosquitto valkey

down:
	docker compose down

seed:
	uv run python scripts/seed.py

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .

typecheck:
	uv run mypy src tests scripts

test:
	uv run pytest

check: lint typecheck test

run:
	uv run uvicorn --factory vigisift.app:create_app --reload

# Don't overwrite: Frigate keeps the camera rules VigiSift pushed in this file.
frigate-config:
	@mkdir -p deploy/frigate/config
	@test -f deploy/frigate/config/config.yml || cp deploy/frigate/config.template.yml deploy/frigate/config/config.yml

# Person detection on the Mac's Neural Engine, outside Docker (README). Listens only on
# 127.0.0.1: the socket has no authentication, and Frigate in Docker still reaches it through
# host.docker.internal.
DETECTOR_DIR ?= ../apple-silicon-detector
detector:
	$(DETECTOR_DIR)/venv/bin/python $(DETECTOR_DIR)/detector/zmq_onnx_client.py \
		--endpoint tcp://127.0.0.1:5555 --model deploy/frigate/config/model_cache/yolov9-t-320.onnx

demo: frigate-config
	docker compose up -d --build --wait
	docker compose exec api python scripts/seed.py

mcp:
	uv run python -m vigisift.mcp_server
