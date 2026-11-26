"""MQTT: HA Discovery 엔티티(스캔 버튼, 자동열기 스위치, 결과 센서, 스캔중 센서)."""
from __future__ import annotations

import asyncio
import json
import logging

import paho.mqtt.client as mqtt

log = logging.getLogger("doorface.mqtt")
P = "doorface"
DEVICE = {"identifiers": ["doorface_addon"], "name": "Doorface 현관 얼굴인식",
          "manufacturer": "wallpad_addons", "model": "doorface"}


class Mqtt:
    def __init__(self, loop: asyncio.AbstractEventLoop, on_scan, on_auto_open):
        self.loop, self.on_scan, self.on_auto_open = loop, on_scan, on_auto_open
        self.c = None
        self.connected = False

    def start(self, cfg: dict):
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="doorface-addon")
        if cfg.get("username"):
            c.username_pw_set(cfg["username"], cfg.get("password"))
        c.will_set(f"{P}/status", "offline", retain=True)
        c.on_connect = self._on_connect
        c.on_disconnect = lambda *a: setattr(self, "connected", False)
        c.on_message = self._on_message
        c.reconnect_delay_set(2, 60)
        c.connect_async(cfg["host"], int(cfg.get("port", 1883)))
        c.loop_start()
        self.c = c

    def _on_connect(self, c, userdata, flags, rc, props=None):
        if rc != 0:
            log.warning("MQTT 연결 실패 rc=%s", rc)
            return
        self.connected = True
        c.subscribe(f"{P}/cmd/#")
        self._discovery()
        c.publish(f"{P}/status", "online", retain=True)
        log.info("MQTT 연결됨")
        asyncio.run_coroutine_threadsafe(self._republish(), self.loop)

    async def _republish(self):
        pass  # main에서 상태 재발행용으로 덮어씀

    def _on_message(self, c, userdata, msg):
        payload = msg.payload.decode(errors="replace").strip()
        if msg.topic == f"{P}/cmd/scan":
            asyncio.run_coroutine_threadsafe(self.on_scan(payload or "mqtt"), self.loop)
        elif msg.topic == f"{P}/cmd/auto_open":
            asyncio.run_coroutine_threadsafe(self.on_auto_open(payload.upper() == "ON"), self.loop)

    def _discovery(self):
        avail = {"availability_topic": f"{P}/status", "device": DEVICE}
        cfgs = {
            "button/doorface/scan": {"name": "얼굴인식 스캔", "unique_id": "doorface_scan", "default_entity_id": "button.doorface_scan",
                                     "command_topic": f"{P}/cmd/scan", "payload_press": "ha",
                                     "icon": "mdi:face-recognition"},
            "switch/doorface/auto_open": {"name": "얼굴인식 자동열기", "unique_id": "doorface_auto_open",
                                          "default_entity_id": "switch.doorface_auto_open",
                                          "command_topic": f"{P}/cmd/auto_open",
                                          "state_topic": f"{P}/state/auto_open", "icon": "mdi:door-open"},
            "sensor/doorface/last": {"name": "얼굴인식 최근결과", "unique_id": "doorface_last", "default_entity_id": "sensor.doorface_last",
                                     "state_topic": f"{P}/state/last",
                                     "json_attributes_topic": f"{P}/state/last_attr",
                                     "icon": "mdi:account-check"},
            "binary_sensor/doorface/scanning": {"name": "얼굴인식 스캔중", "unique_id": "doorface_scanning",
                                                "default_entity_id": "binary_sensor.doorface_scanning",
                                                "state_topic": f"{P}/state/scanning", "icon": "mdi:cctv"},
        }
        for path, c in cfgs.items():
            self.c.publish(f"homeassistant/{path}/config", json.dumps({**c, **avail}, ensure_ascii=False),
                           retain=True)

    def pub(self, topic, payload, retain=True):
        if self.c and self.connected:
            if not isinstance(payload, str):
                payload = json.dumps(payload, ensure_ascii=False)
            self.c.publish(f"{P}/{topic}", payload, retain=retain)
