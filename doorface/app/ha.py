"""Home Assistant(Supervisor 프록시) 연동: 엔티티 상태 조회, 서비스 호출, 이벤트 발생, MQTT 정보."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request

log = logging.getLogger("doorface.ha")
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
BASE = "http://supervisor"


def _req(method, path, body=None, timeout=5):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method, headers={
        "Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


async def call(method, path, body=None):
    if not TOKEN:
        raise RuntimeError("SUPERVISOR_TOKEN 없음(애드온 밖에서 실행 중)")
    return await asyncio.to_thread(_req, method, path, body)


async def state(entity_id: str):
    try:
        return await call("GET", f"/core/api/states/{entity_id}")
    except Exception as e:  # noqa: BLE001
        log.warning("상태 조회 실패 %s: %s", entity_id, e)
        return None


async def run_script(entity_id: str, variables: dict):
    domain, _ = entity_id.split(".", 1)
    if domain == "script":
        body = {"entity_id": entity_id, "variables": variables}
        return await call("POST", "/core/api/services/script/turn_on", body)
    # 버튼/스위치 등도 허용
    svc = {"button": "press", "switch": "turn_on", "lock": "unlock"}.get(domain, "turn_on")
    return await call("POST", f"/core/api/services/{domain}/{svc}", {"entity_id": entity_id})


async def fire_event(name: str, data: dict):
    try:
        await call("POST", f"/core/api/events/{name}", data)
    except Exception as e:  # noqa: BLE001
        log.debug("이벤트 발생 실패: %s", e)


async def mqtt_service():
    try:
        r = await call("GET", "/services/mqtt")
        return (r or {}).get("data")
    except Exception as e:  # noqa: BLE001
        log.warning("MQTT 서비스 정보 없음: %s", e)
        return None
