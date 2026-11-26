"""Doorface 애드온 메인: 웹 UI(ingress) + API + MQTT."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import urllib.request
import zipfile
from contextlib import asynccontextmanager

import cv2
import numpy as np
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from . import ha
from .capture import Capturer
from .engine import Engine
from .mqttc import Mqtt
from .scanner import Scanner
from .store import Events, People, Settings

DATA = os.environ.get("DOORFACE_DATA", "/data")


def _options():
    try:
        with open(os.path.join(DATA, "options.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


logging.basicConfig(level=_options().get("log_level", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("doorface")

HERE = os.path.dirname(__file__)
PACKS = {"buffalo_l": ("det_10g.onnx", "w600k_r50.onnx"),
         "buffalo_s": ("det_500m.onnx", "w600k_mbf.onnx")}
URL = "https://github.com/deepinsight/insightface/releases/download/v0.7/{}.zip"
ALLOWED = {"172.30.32.2", "127.0.0.1"}   # HA ingress 프록시만 허용


class State:
    def __init__(self):
        os.makedirs(DATA, exist_ok=True)
        self.data_dir = DATA
        self.opts = _options()
        self.pack = self.opts.get("model", "buffalo_l")
        if self.pack not in PACKS:
            self.pack = "buffalo_l"
        self.settings = Settings(DATA)
        self.people = People(DATA)
        self.events = Events(DATA)
        self.capturer = Capturer(DATA, self.settings)
        self.engine: Engine | None = None
        self.model_state = "대기"
        self.mqtt: Mqtt | None = None
        self.scanner = Scanner(self)
        self.last = None

    # ---- 모델 ----
    def _model_dir(self):
        return os.environ.get("DOORFACE_MODEL_DIR") or os.path.join(DATA, "models", self.pack)

    def start_load(self):
        """모델 다운로드/로드를 백그라운드로 시작 (이미 진행 중이면 무시)."""
        if self.engine is not None or getattr(self, "_loading", False):
            return False
        self._loading = True

        async def run():
            try:
                await asyncio.to_thread(self.load_model)
            except Exception as e:  # noqa: BLE001
                self.model_state = f"모델 오류: {e}"
                log.exception("모델 로드 실패")
            finally:
                self._loading = False
        asyncio.create_task(run())
        return True

    def _download(self, url, dst, tries=8):
        """끊기면 받은 데까지 이어받기(Range) + 재시도."""
        total = None
        for attempt in range(1, tries + 1):
            have = os.path.getsize(dst) if os.path.exists(dst) else 0
            if total and have >= total:
                return
            headers = {"User-Agent": "doorface-addon"}
            if have:
                headers["Range"] = f"bytes={have}-"
            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=60) as r:
                    if have and r.status == 206:
                        cr = r.headers.get("Content-Range", "")
                        total = int(cr.split("/")[-1]) if "/" in cr else None
                        mode = "ab"
                    else:
                        have, mode = 0, "wb"
                        total = int(r.headers.get("Content-Length") or 0) or None
                    with open(dst, mode) as f:
                        while chunk := r.read(1 << 20):
                            f.write(chunk)
                            have += len(chunk)
                            pct = f"{have * 100 // total}%" if total else f"{have >> 20}MB"
                            self.model_state = f"모델 다운로드 중 {pct} ({self.pack})"
                if total is None or have >= total:
                    return
            except Exception as e:  # noqa: BLE001
                log.warning("모델 다운로드 끊김(%s) — 이어받기 재시도 %d/%d", e, attempt, tries)
                self.model_state = f"다운로드 재시도 {attempt}/{tries}"
                time.sleep(min(30, 3 * attempt))
        raise RuntimeError("모델 다운로드 반복 실패 — 네트워크 확인 후 다시 시도")

    def load_model(self):
        d = self._model_dir()
        det, rec = PACKS[self.pack]
        if not (os.path.exists(os.path.join(d, det)) and os.path.exists(os.path.join(d, rec))):
            os.makedirs(d, exist_ok=True)
            zp = os.path.join(d, "pack.zip.part")
            for attempt in range(1, 4):
                self._download(URL.format(self.pack), zp)
                try:
                    with zipfile.ZipFile(zp) as z:
                        for name in z.namelist():
                            base = os.path.basename(name)
                            if base in (det, rec):
                                with z.open(name) as src, open(os.path.join(d, base) + ".tmp", "wb") as dst:
                                    while chunk := src.read(1 << 20):
                                        dst.write(chunk)
                                os.replace(os.path.join(d, base) + ".tmp", os.path.join(d, base))
                    break
                except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError) as e:
                    log.warning("모델 압축 파일 손상(%s) — 처음부터 다시 받음 (%d/3)", e, attempt)
                    os.remove(zp)
                    if attempt == 3:
                        raise
            os.remove(zp)
        self.model_state = "모델 로딩 중"
        eng = Engine(d, det, rec)

        def emb_from_crop(path):
            img = cv2.imread(path)
            return eng.rec.embed([cv2.resize(img, (112, 112))])[0]

        n = self.people.reembed(self.pack, emb_from_crop)
        if n:
            log.info("모델 변경으로 샘플 %d개 재계산", n)
        self.engine = eng
        self.model_state = "준비됨"
        log.info("모델 준비됨: %s", self.pack)

    # ---- 알림 ----
    async def notify_scanning(self, on: bool):
        if self.mqtt:
            self.mqtt.pub("state/scanning", "ON" if on else "OFF")

    async def notify_result(self, r: dict):
        self.last = r
        name = r.get("person") if r.get("result") == "match" else r.get("result")
        if self.mqtt:
            self.mqtt.pub("state/last", name or "none")
            self.mqtt.pub("state/last_attr", {k: v for k, v in r.items() if k != "candidates"})
        await ha.fire_event("doorface_result", r)

    async def publish_state(self):
        if self.mqtt:
            self.mqtt.pub("state/auto_open", "ON" if self.settings.get()["door"]["auto_open"] else "OFF")
            self.mqtt.pub("state/scanning", "ON" if self.scanner.busy else "OFF")

    async def set_auto_open(self, on: bool):
        self.settings.update({"door": {"auto_open": bool(on)}})
        await self.publish_state()

    async def trigger_scan(self, trigger="api"):
        if self.scanner.busy:
            return False
        asyncio.create_task(self.scanner.run("auto", trigger))
        return True


S: State | None = None


async def _housekeeping():
    while True:
        try:
            S.scanner.clean_tuning()
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(app):
    global S
    S = State()
    await S.capturer.ensure_key()
    loop = asyncio.get_running_loop()

    S.start_load()

    mcfg = await ha.mqtt_service() if os.environ.get("SUPERVISOR_TOKEN") else None
    if mcfg:
        S.mqtt = Mqtt(loop, S.trigger_scan, S.set_auto_open)
        S.mqtt._republish = S.publish_state
        S.mqtt.start(mcfg)
    else:
        log.warning("MQTT 미사용(서비스 정보 없음)")
    hk = asyncio.create_task(_housekeeping())
    yield
    hk.cancel()
    if S.mqtt and S.mqtt.c:
        S.mqtt.pub("status", "offline")
        S.mqtt.c.loop_stop()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def only_ingress(request: Request, call_next):
    if not os.environ.get("DOORFACE_DEV"):
        host = request.client.host if request.client else ""
        if host not in ALLOWED:
            return JSONResponse({"error": "forbidden"}, status_code=403)
    return await call_next(request)


# ---------------- 페이지 ----------------
@app.get("/")
async def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"),
                        headers={"Cache-Control": "no-store"})


# ---------------- 상태/설정 ----------------
@app.get("/api/status")
async def status():
    persons, M, _ = S.people.gallery()
    return {"model": S.pack, "model_state": S.model_state, "mqtt": bool(S.mqtt and S.mqtt.connected),
            "pubkey": S.capturer.public_key(), "busy": S.scanner.busy, "people": len(S.people.data["people"]),
            "samples": int(len(M)), "settings": S.settings.get(), "last": S.last,
            "mock": bool(S.capturer.mock_dir)}


@app.post("/api/settings")
async def set_settings(patch: dict):
    st = S.settings.update(patch)
    await S.publish_state()
    return st


@app.post("/api/model/retry")
async def model_retry():
    return {"started": S.start_load(), "state": S.model_state}


@app.post("/api/mango/test")
async def mango_test():
    return await S.capturer.test()


# ---------------- 스캔 ----------------
@app.post("/api/scan")
async def scan(body: dict):
    mode = body.get("mode", "test")
    if mode not in ("test", "auto", "enroll"):
        raise HTTPException(400, "mode")
    if S.engine is None:
        raise HTTPException(409, S.model_state)
    if S.scanner.busy:
        raise HTTPException(409, "이미 스캔 중")
    delay = max(0, min(int(body.get("delay", 0)), 60))
    frames = body.get("frames")
    asyncio.create_task(S.scanner.run(mode, "ui", delay, frames))
    await asyncio.sleep(0.05)
    return {"ok": True, "session": S.scanner.session["id"] if S.scanner.session else None}


@app.get("/api/scan/session")
async def scan_session(since: int = 0):
    return S.scanner.session_view(since) or {}


@app.post("/api/scan/cancel")
async def scan_cancel():
    S.scanner.cancel()
    return {"ok": True}


# ---------------- 사람 ----------------
@app.get("/api/people")
async def people():
    return S.people.list()


@app.post("/api/people")
async def people_add(body: dict):
    return S.people.add(body.get("name", ""), body.get("policy", "phone"))


@app.patch("/api/people/{pid}")
async def people_update(pid: str, body: dict):
    p = S.people.update(pid, body)
    if not p:
        raise HTTPException(404)
    return {k: v for k, v in p.items() if k != "samples"}


@app.delete("/api/people/{pid}")
async def people_delete(pid: str):
    if not S.people.delete(pid):
        raise HTTPException(404)
    return {"ok": True}


@app.post("/api/people/{pid}/samples")
async def samples_add(pid: str, body: dict):
    added = []
    for cid in body.get("cids", []):
        c = S.scanner.candidates.get(cid)
        if c:
            s = S.people.add_sample(pid, c["crop"], c["emb"], c["q"])
            if s:
                added.append(s["id"])
    if not added:
        raise HTTPException(400, "추가된 사진 없음")
    return {"added": added}


@app.post("/api/people/{pid}/upload")
async def samples_upload(pid: str, file: UploadFile = File(...)):
    if S.engine is None:
        raise HTTPException(409, S.model_state)
    raw = await file.read()
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(400, "이미지를 읽을 수 없음")
    faces = await asyncio.to_thread(S.engine.analyze, img)
    if not faces:
        raise HTTPException(400, "얼굴을 찾지 못함")
    f = faces[0]
    crop = cv2.imencode(".jpg", f.aligned, [cv2.IMWRITE_JPEG_QUALITY, 95])[1].tobytes()
    s = S.people.add_sample(pid, crop, f.embedding, f.quality)
    if not s:
        raise HTTPException(404)
    return {"added": [s["id"]], "q": f.quality}


@app.delete("/api/people/{pid}/samples/{sid}")
async def samples_delete(pid: str, sid: str):
    if not S.people.delete_sample(pid, sid):
        raise HTTPException(404)
    return {"ok": True}


@app.get("/api/faces/{fn}")
async def face_img(fn: str):
    if not re.fullmatch(r"[0-9a-f]{8}_[0-9a-f]{10}\.jpg", fn):
        raise HTTPException(400)
    path = os.path.join(S.people.dir, fn)
    if not os.path.exists(path):
        raise HTTPException(404)
    return FileResponse(path)


# ---------------- 기록/튜닝 ----------------
@app.get("/api/events")
async def events(n: int = 100):
    return S.events.recent(min(n, 500))


@app.get("/api/tuning")
async def tuning():
    root = os.path.join(DATA, "tuning")
    dirs = sorted(os.listdir(root)) if os.path.isdir(root) else []
    return {"sessions": len(dirs), "latest": dirs[-1] if dirs else None}


@app.post("/api/tuning/clear")
async def tuning_clear():
    S.scanner.clean_tuning(days=0)
    return {"ok": True, "ts": time.time()}
