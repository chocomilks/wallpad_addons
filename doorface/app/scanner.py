"""스캔 세션: 캡처 → 인식 → 판정(→ 문열기). 모드: auto(실사용) / test(문 안 엶) / enroll(등록용 촬영)."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
import time
import uuid

import cv2
import numpy as np

from . import ha

log = logging.getLogger("doorface.scan")


def _jpg(img, width=None, q=85) -> bytes:
    if width and img.shape[1] > width:
        h = int(img.shape[0] * width / img.shape[1])
        img = cv2.resize(img, (width, h), interpolation=cv2.INTER_AREA)
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])[1].tobytes()


def _data_url(b: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(b).decode()


class Scanner:
    def __init__(self, app_state):
        self.s = app_state          # engine/capturer/settings/people/events/notify
        self.lock = asyncio.Lock()
        self.session: dict | None = None
        self.candidates: dict[str, dict] = {}   # 등록 후보 (메모리에만)
        self._cancel = False

    @property
    def busy(self):
        return self.lock.locked()

    def cancel(self):
        self._cancel = True

    # ------------------------------------------------------------------
    async def run(self, mode="auto", trigger="manual", delay=0, frames=None):
        if self.lock.locked():
            return {"error": "busy"}
        async with self.lock:
            self._cancel = False
            st = self.s.settings.get()
            cap, m = st["capture"], st["match"]
            n = int(frames or cap["max_frames"])
            sess = {"id": uuid.uuid4().hex[:8], "mode": mode, "trigger": trigger, "state": "starting",
                    "started": time.time(), "frames": [], "result": None, "countdown": int(delay)}
            self.session = sess
            if mode == "enroll":
                self.candidates.clear()
            await self.s.notify_scanning(True)
            try:
                if self.s.engine is None:
                    raise RuntimeError("모델 준비 중")
                for i in range(int(delay), 0, -1):
                    if self._cancel:
                        break
                    sess["countdown"] = i
                    await asyncio.sleep(1)
                sess["countdown"] = 0
                sess["state"] = "capturing"
                result = await self._loop(sess, mode, n, cap, m, st)
            except Exception as e:  # noqa: BLE001
                log.exception("스캔 실패")
                result = {"result": "error", "detail": str(e)}
            result.update({"mode": mode, "trigger": trigger, "frames": len(sess["frames"]),
                           "duration": round(time.time() - sess["started"], 2),
                           "capture": sess.get("capture")})
            sess["result"], sess["state"] = result, "done"
            if mode != "enroll":
                self.s.events.add(result)
                await self.s.notify_result(result)
            await self.s.notify_scanning(False)
            return result

    # ------------------------------------------------------------------
    async def _loop(self, sess, mode, n, cap, m, st):
        # 실사용/테스트는 최신 프레임 위주(밀리면 버림), 등록 촬영은 전부 보존
        q: asyncio.Queue = asyncio.Queue(maxsize=0 if mode == "enroll" else 2)
        t0 = time.time()

        stats = {"ok": 0, "empty": 0, "partial": 0, "dropped": 0, "first_at": None, "cap_sum": 0.0}
        sess["capture"] = stats
        hard_limit = cap["warmup"] + cap["window_sec"] + 15   # 첫 프레임이 영영 안 와도 여기서 끝

        async def producer():
            try:
                async for fr in self.s.capturer.frames(n, int(cap["warmup"])):
                    stats[fr["status"]] += 1
                    stats["cap_sum"] = round(stats["cap_sum"] + fr["cap"], 2)
                    if self._cancel or time.time() - t0 > hard_limit:
                        break
                    if fr["jpg"] is None:
                        continue
                    if stats["first_at"] is None:
                        stats["first_at"] = round(time.time() - t0, 2)
                    if q.full():          # 분석이 밀리면 오래된 프레임 버림
                        q.get_nowait()
                        stats["dropped"] += 1
                    await q.put(fr)
            finally:
                if q.full():
                    q.get_nowait()
                q.put_nowait(None)

        prod = asyncio.create_task(producer())
        tuning_dir = None
        if mode == "auto" and st.get("tuning_save"):
            tuning_dir = os.path.join(self.s.data_dir, "tuning", time.strftime("%Y%m%d-%H%M%S"))
            os.makedirs(tuning_dir, exist_ok=True)

        cur_pid, cnt = None, 0
        best_seen = {"name": None, "sim": 0.0}
        phone_missing = None
        decision = None
        idx = 0
        try:
            while True:
                item = await q.get()
                if item is None or self._cancel:
                    break
                # 판정 창은 첫 정상 프레임 도착부터 계산 (호출·영상 준비 시간은 제외)
                if mode != "enroll" and stats["first_at"] is not None and \
                        time.time() - t0 - stats["first_at"] > cap["window_sec"]:
                    break
                jpg = item["jpg"]
                idx += 1
                img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
                if img is None:
                    stats["partial"] += 1
                    continue
                ta = time.time()
                faces = await asyncio.to_thread(self.s.engine.analyze, img, m.get("det_size", 320))
                fr = {"i": idx, "t": round(time.time() - t0, 2), "face": bool(faces),
                      "cap": item["cap"], "proc": round(time.time() - ta, 2)}
                if tuning_dir:
                    with open(os.path.join(tuning_dir, f"{idx:02d}.jpg"), "wb") as f:
                        f.write(jpg)
                if mode in ("test", "enroll"):
                    fr["thumb"] = _data_url(_jpg(img, 320, 80))
                if not faces:
                    sess["frames"].append(fr)
                    continue
                f = faces[0]
                fr["q"] = f.quality
                qok = (f.quality["size"] >= m["min_face"] and f.quality["yaw"] <= m["max_yaw"]
                       and f.quality["pitch"] <= m["max_pitch"])
                fr["quality_ok"] = qok

                if mode == "enroll":
                    cid = uuid.uuid4().hex[:8]
                    crop = _jpg(f.aligned, None, 95)
                    self.candidates[cid] = {"emb": f.embedding, "q": f.quality, "crop": crop}
                    fr["cid"] = cid
                    fr["crop"] = _data_url(crop)
                    sess["frames"].append(fr)
                    continue

                persons, M, owner = self.s.people.gallery()
                if not len(M):
                    sess["frames"].append(fr)
                    decision = decision or {"result": "no_people"}
                    continue
                sims = M @ f.embedding
                per = np.full(len(persons), -1.0, np.float32)
                np.maximum.at(per, owner, sims)
                order = np.argsort(-per)
                b = int(order[0])
                bsim = float(per[b])
                ssim = float(per[order[1]]) if len(order) > 1 else -1.0
                p = persons[b]
                strict = p.get("policy") == "strict"
                thr = m["thr_strict"] if strict else m["thr_phone"]
                need = int(m["consec_strict"] if strict else m["consec_phone"])
                passed = qok and bsim >= thr and (bsim - ssim) >= m["min_margin"]
                fr.update({"best": p["name"], "sim": round(bsim, 3), "second": round(ssim, 3),
                           "thr": thr, "pass": passed})
                if bsim > best_seen["sim"]:
                    best_seen = {"name": p["name"], "sim": round(bsim, 3)}
                if passed:
                    cnt = cnt + 1 if cur_pid == p["id"] else 1
                    cur_pid = p["id"]
                elif qok:
                    cur_pid, cnt = None, 0
                fr["streak"] = cnt
                sess["frames"].append(fr)

                if passed and cnt >= need:
                    if not strict:
                        ok, detail = await self._phone_ok(p)
                        fr["phone"] = detail
                        if not ok:
                            phone_missing = {"person": p["name"], "detail": detail}
                            continue    # 폰이 아직 안 잡힘 → 창 안에서 계속 시도
                    decision = {"result": "match", "person": p["name"], "person_id": p["id"],
                                "policy": p.get("policy"), "sim": round(bsim, 3), "streak": cnt}
                    break
        finally:
            prod.cancel()
            try:
                await prod
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._cancel = False

        if mode == "enroll":
            return {"result": "enroll", "candidates": len(self.candidates)}
        if decision and decision["result"] == "match":
            opened = False
            if mode == "auto" and st["door"]["auto_open"]:
                try:
                    await ha.run_script(st["door"]["script"], {"person": decision["person"],
                                                               "similarity": decision["sim"]})
                    opened = True
                except Exception as e:  # noqa: BLE001
                    decision["open_error"] = str(e)
            decision["opened"] = opened
            return decision
        if decision:
            return decision
        if phone_missing:
            return {"result": "phone_missing", **phone_missing, "best": best_seen}
        faces_seen = sum(1 for x in sess["frames"] if x.get("face"))
        return {"result": "no_match" if faces_seen else "no_face", "best": best_seen}

    async def _phone_ok(self, p):
        ent = (p.get("phone_entity") or "").strip()
        if not ent:
            return False, "폰 엔티티 미지정"
        stt = await ha.state(ent)
        if not stt:
            return False, f"{ent} 조회 실패"
        want = [x.strip().lower() for x in (p.get("phone_states") or "home").split(",") if x.strip()]
        cur = str(stt.get("state", "")).lower()
        return cur in want, f"{ent}={stt.get('state')}"

    # ------------------------------------------------------------------
    def clean_tuning(self, days=7):
        root = os.path.join(self.s.data_dir, "tuning")
        if not os.path.isdir(root):
            return
        cutoff = time.time() - days * 86400
        for d in os.listdir(root):
            p = os.path.join(root, d)
            if os.path.getmtime(p) < cutoff:
                shutil.rmtree(p, ignore_errors=True)

    def session_view(self, since=0):
        s = self.session
        if not s:
            return None
        v = {k: v for k, v in s.items() if k != "frames"}
        v["frames"] = s["frames"][since:]
        v["total"] = len(s["frames"])
        return json.loads(json.dumps(v, default=float))
