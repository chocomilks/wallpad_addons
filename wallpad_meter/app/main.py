"""Wallpad Meter 애드온 메인: 주기적으로 원격검침을 읽어 MQTT(retained JSON + Discovery)로 올린다."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request
from datetime import datetime, timedelta, timezone

import paho.mqtt.client as mqtt

from .fetch import Fetcher, check_options, page_urls
from .parse import ITEMS, KEYS
from .state import build

DATA = os.environ.get("METER_DATA", "/data")
P = "wallpad_meter"
STATE = f"{P}/state"
HEALTH = f"{P}/health"
DEVICE = {"identifiers": ["wallpad_meter_addon"], "name": "월패드 원격검침",
          "manufacturer": "wallpad_addons", "model": "wallpad_meter"}
ICONS = {"elec": "mdi:flash", "gas": "mdi:fire", "water": "mdi:water",
         "hotwater": "mdi:water-thermometer", "heating": "mdi:radiator"}

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.environ.get("TZ") or "Asia/Seoul")
except Exception:  # noqa: BLE001
    TZ = timezone(timedelta(hours=9))


def load_json(name: str, default=None):
    try:
        with open(os.path.join(DATA, name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(name: str, data) -> None:
    path = os.path.join(DATA, name)
    with open(path + ".tmp", "w") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(path + ".tmp", path)


OPTS = load_json("options.json", {})
logging.basicConfig(level=OPTS.get("log_level", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("wallpad_meter")


def mqtt_config() -> dict:
    if os.environ.get("MQTT_HOST"):
        return {"host": os.environ["MQTT_HOST"], "port": int(os.environ.get("MQTT_PORT", 1883)),
                "username": os.environ.get("MQTT_USER"), "password": os.environ.get("MQTT_PASS")}
    req = urllib.request.Request("http://supervisor/services/mqtt", headers={
        "Authorization": f"Bearer {os.environ.get('SUPERVISOR_TOKEN', '')}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())["data"]


def discovery() -> dict[str, dict]:
    avail = {"availability_topic": f"{P}/status", "device": DEVICE}
    cfgs: dict[str, dict] = {}
    for key, name, unit, dc in ITEMS:
        # 항목이 아직 없어도 템플릿이 깨지지 않게 .get 사용 (None → HA에선 '알 수 없음')
        v = "(value_json['items'].get('" + key + "') or {})"
        common = {"state_topic": STATE, "icon": ICONS[key]}
        cfgs[f"sensor/{P}/{key}_month"] = {
            **common, "name": f"{name} 이번달 사용량", "unique_id": f"{P}_{key}_month",
            "default_entity_id": f"sensor.{P}_{key}_month",
            "value_template": "{{ " + v + ".get('month') }}",
            "unit_of_measurement": unit, "device_class": dc, "state_class": "total_increasing",
            "json_attributes_topic": STATE,
            "json_attributes_template": "{{ " + v + " | tojson }}"}
        cfgs[f"sensor/{P}/{key}_cost"] = {
            **common, "name": f"{name} 이번달 요금", "unique_id": f"{P}_{key}_cost",
            "default_entity_id": f"sensor.{P}_{key}_cost",
            "value_template": "{{ " + v + ".get('cost') }}", "unit_of_measurement": "원",
            "icon": "mdi:cash",
            "json_attributes_topic": STATE,
            "json_attributes_template":
                "{{ {'지난달': " + v + ".get('prev_cost'), '월말 예상': " + v + ".get('projected_cost')} | tojson }}"}
        cfgs[f"sensor/{P}/{key}_total"] = {
            **common, "name": f"{name} 누적", "unique_id": f"{P}_{key}_total",
            "default_entity_id": f"sensor.{P}_{key}_total",
            "value_template": "{{ " + v + ".get('total') }}",
            "unit_of_measurement": unit, "device_class": dc, "state_class": "total_increasing"}
    cfgs[f"sensor/{P}/cost_total"] = {
        "name": "공과금 이번달 합계", "unique_id": f"{P}_cost_total",
        "default_entity_id": f"sensor.{P}_cost_total", "state_topic": STATE,
        "value_template": "{{ value_json['cost_total'] }}", "unit_of_measurement": "원",
        "icon": "mdi:cash-multiple", "json_attributes_topic": STATE,
        "json_attributes_template":
            "{{ {'지난달': value_json['prev_cost_total'], '월말 예상': value_json['projected_cost_total'],"
            " '갱신': value_json['updated']} | tojson }}"}
    cfgs[f"sensor/{P}/status"] = {
        "name": "원격검침 상태", "unique_id": f"{P}_status", "default_entity_id": f"sensor.{P}_status",
        "state_topic": HEALTH, "value_template": "{{ value_json['state'] }}",
        "json_attributes_topic": HEALTH, "icon": "mdi:meter-electric", "entity_category": "diagnostic"}
    cfgs[f"button/{P}/refresh"] = {
        "name": "원격검침 새로고침", "unique_id": f"{P}_refresh", "default_entity_id": f"button.{P}_refresh",
        "command_topic": f"{P}/cmd/refresh", "payload_press": "refresh", "icon": "mdi:refresh"}
    return {path: {**c, **avail} for path, c in cfgs.items()}


class App:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.refresh = asyncio.Event()
        self.fetcher = Fetcher(DATA)
        self.last = load_json("last.json")
        self.health = {"state": "시작 중"}
        self.pubkey = ""
        self.client: mqtt.Client | None = None

    # ---------- MQTT ----------
    def start_mqtt(self, cfg: dict):
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="wallpad-meter-addon")
        if cfg.get("username"):
            c.username_pw_set(cfg["username"], cfg.get("password"))
        c.will_set(f"{P}/status", "offline", retain=True)
        c.on_connect = self._on_connect
        c.on_message = lambda *_a: self.loop.call_soon_threadsafe(self.refresh.set)
        c.reconnect_delay_set(2, 60)
        c.connect_async(cfg["host"], int(cfg.get("port", 1883)))
        c.loop_start()
        self.client = c

    def _on_connect(self, c, _u, _f, rc, _p=None):
        if rc != 0:
            log.warning("MQTT 연결 실패 rc=%s", rc)
            return
        c.subscribe(f"{P}/cmd/refresh")
        for path, cfg in discovery().items():
            c.publish(f"homeassistant/{path}/config", json.dumps(cfg, ensure_ascii=False), retain=True)
        c.publish(f"{P}/status", "online", retain=True)
        if self.last:
            c.publish(STATE, json.dumps(self.last, ensure_ascii=False), retain=True)
        c.publish(HEALTH, json.dumps(self.health, ensure_ascii=False), retain=True)
        log.info("MQTT 연결됨")

    def pub(self, topic: str, data: dict):
        if self.client:
            self.client.publish(topic, json.dumps(data, ensure_ascii=False), retain=True)

    def set_health(self, state: str, **extra):
        self.health = {"state": state, **extra, "checked": datetime.now(TZ).isoformat(timespec="seconds"),
                       "ssh_public_key": self.pubkey,
                       "last_success": (self.last or {}).get("updated")}
        self.pub(HEALTH, self.health)

    # ---------- 검침 1회 ----------
    async def run_once(self, o: dict):
        bad = check_options(o)
        if bad:
            log.error("옵션을 채워주세요: %s", ", ".join(bad))
            self.set_health("설정 필요", error="옵션 확인: " + ", ".join(bad))
            return
        now = datetime.now(TZ)
        try:
            pages = await self.fetcher.fetch(o, page_urls(o, now.year, now.month, KEYS))
        except Exception as e:  # noqa: BLE001
            log.warning("검침 실패: %s", e)
            self.set_health("오류", error=str(e))
            return
        state, failed = build(pages, now, o, self.last)
        if not state["items"]:
            self.set_health("오류", error="포털 응답에서 값을 찾지 못함 (월패드·단지서버·옵션 확인)")
            return
        self.last = state
        save_json("last.json", state)
        self.pub(STATE, state)
        if failed:
            self.set_health("일부 실패", error="못 받은 항목: " + ", ".join(failed))
        else:
            self.set_health("정상")
        e = state["items"].get("elec", {})
        log.info("검침 완료: 전기 이번달 %s kWh / %s원, 합계 %s원",
                 e.get("month"), e.get("cost"), state.get("cost_total"))

    async def run(self):
        self.pubkey = await self.fetcher.ensure_key()
        log.info("망고 /etc/dropbear/authorized_keys 에 아래 공개키를 한 줄로 추가하세요:\n%s", self.pubkey)
        try:
            self.start_mqtt(mqtt_config())
        except Exception as e:  # noqa: BLE001
            log.error("MQTT 설정을 가져오지 못함: %s (Mosquitto 애드온/MQTT 통합 확인)", e)
        await asyncio.sleep(3)
        interval = max(30, int(OPTS.get("interval_minutes", 180))) * 60
        while True:
            await self.run_once(OPTS)
            try:
                await asyncio.wait_for(self.refresh.wait(), interval)
                log.info("새로고침 요청")
            except asyncio.TimeoutError:
                pass
            self.refresh.clear()


async def _main():
    await App().run()


if __name__ == "__main__":
    asyncio.run(_main())
