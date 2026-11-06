"""Simulate a device: report online, send telemetry, listen for commands, ack them.

uv run python scripts/simulate_device.py lamp-1

Stop it with `kill -9` (not Ctrl-C) to see the Last Will: the broker reports
`offline` for the device after about 1.5 keepalive periods.
"""

import argparse
import json
import time

import paho.mqtt.client as paho
from paho.mqtt.enums import CallbackAPIVersion

parser = argparse.ArgumentParser()
parser.add_argument("device_id", nargs="?", default="lamp-1")
parser.add_argument("--password", default="devpass")
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=1883)
parser.add_argument(
    "--heartbeat", type=float, default=10, help="seconds between telemetry messages"
)
args = parser.parse_args()

device_id: str = args.device_id
state = "ON" if device_id.startswith("lamp") else "OFF"
seen_commands: set[str] = set()

client = paho.Client(CallbackAPIVersion.VERSION2, client_id=f"device-{device_id}")
client.username_pw_set(device_id, args.password)
# Last Will: registered before connecting. When a connection dies without notice, the
# broker publishes for the device. Retained so later subscribers see it right away.
client.will_set(f"device/{device_id}/status", "offline", qos=1, retain=True)


def on_connect(c: paho.Client, _u: object, _f: object, reason: object, _p: object) -> None:
    c.publish(f"device/{device_id}/status", "online", qos=1, retain=True)
    c.subscribe(f"device/{device_id}/command", qos=1)
    print(f"{device_id} online ({reason})")


def on_command(c: paho.Client, _u: object, message: paho.MQTTMessage) -> None:
    global state
    command = json.loads(message.payload.decode())
    command_id = command["command_id"]
    # QoS 1 can deliver the same command again. If already done, only ack again, without
    # doing it twice.
    if command_id not in seen_commands:
        seen_commands.add(command_id)
        state = command["action"]
        print(f"  ← command {command_id[:8]}: {state}")
    else:
        print(f"  ← command {command_id[:8]} duplicate, ignored")
    c.publish(f"device/{device_id}/telemetry", json.dumps({"state": state}), qos=1)
    c.publish(f"device/{device_id}/ack", json.dumps({"command_id": command_id, "ok": True}), qos=1)


client.on_connect = on_connect
client.on_message = on_command
client.connect(args.host, args.port, keepalive=5)
client.loop_start()

try:
    while True:
        client.publish(f"device/{device_id}/telemetry", json.dumps({"state": state}), qos=1)
        time.sleep(args.heartbeat)
except KeyboardInterrupt:
    # Ctrl-C is a clean stop: it reports offline itself, so the broker needs no Last Will.
    client.publish(f"device/{device_id}/status", "offline", qos=1, retain=True).wait_for_publish()
    client.disconnect()
    client.loop_stop()
