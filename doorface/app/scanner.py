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
from datetime import datetime

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


MAX_FACES = 4   # 한 프레임에서 판정할 최대 얼굴 수 (큰 순)


def _annotate(img, faces, judged):
    """테스트 화면용: 얼굴마다 상자와 번호 (초록=통과, 빨강=불일치, 회색=품질 미달)."""
    out = img.copy()
    for k, (f, j) in enumerate(zip(faces, judged)):
        color = (60, 200, 60) if j["pass"] else ((60, 60, 220) if j["qok"] else (150, 150, 150))
        x1, y1, x2, y2 = [int(v) for v in f.bbox]
        cv2.rectangle(out, (x1, y1), (x2, y2), color, 3)
        cv2.putText(out, str(k + 1), (x1 + 4, max(24, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.9, color, 2)
    return out


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
                skip = await self._door_guard(st) if mode == "auto" else None
                if skip:
                    result = skip
                else:
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
        door = {"opened": False}
        watch = None
        sensor = (st["door"].get("sensor") or "").strip()
        if mode == "auto" and sensor:
            async def watcher():
                # 스캔 중 문이 열리면(안에서 열어줌·비밀번호 등) 즉시 중지
                while True:
                    await asyncio.sleep(0.5)
                    stt = await ha.state(sensor)
                    if stt and stt.get("state") == "on":
                        door["opened"] = True
                        self._cancel = True
                        log.info("스캔 중 현관문 열림 → 중지")
                        if q.empty():
                            q.put_nowait(None)   # 대기 중인 소비자 깨우기
                        return
            watch = asyncio.create_task(watcher())
        tuning_dir = None
        if mode == "auto" and st.get("tuning_save"):
            tuning_dir = os.path.join(self.s.data_dir, "tuning", time.strftime("%Y%m%d-%H%M%S"))
            os.makedirs(tuning_dir, exist_ok=True)

        streak: dict[str, int] = {}   # 사람별 연속 일치 횟수
        miss: dict[str, int] = {}     # 사람별 연속 불일치 프레임 (1번까지 허용)
        best_seen = {"name": None, "sim": 0.0}
        phone_missing = None
        decision = None
        idx = 0
        faces_max = 0
        det_size = m.get("det_size", 320)
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
                faces = await asyncio.to_thread(self.s.engine.analyze, img, det_size, 0.5,
                                                1 if mode == "enroll" else MAX_FACES)
                fr = {"i": idx, "t": round(time.time() - t0, 2), "face": bool(faces),
                      "cap": item["cap"], "proc": round(time.time() - ta, 2)}
                faces_max = max(faces_max, len(faces))
                if tuning_dir:
                    with open(os.path.join(tuning_dir, f"{idx:02d}.jpg"), "wb") as f:
                        f.write(jpg)
                if not faces:
                    if mode in ("test", "enroll"):
                        fr["thumb"] = _data_url(_jpg(img, 320, 80))
                    sess["frames"].append(fr)
                    continue

                if mode == "enroll":
                    f = faces[0]
                    fr["q"] = f.quality
                    fr["quality_ok"] = self._qok(f, m)
                    fr["thumb"] = _data_url(_jpg(img, 320, 80))
                    cid = uuid.uuid4().hex[:8]
                    crop = _jpg(f.aligned, None, 95)
                    self.candidates[cid] = {"emb": f.embedding, "q": f.quality, "crop": crop}
                    fr["cid"] = cid
                    fr["crop"] = _data_url(crop)
                    sess["frames"].append(fr)
                    continue

                persons, M, owner = self.s.people.gallery()
                if not len(M):
                    if mode == "test":
                        fr["thumb"] = _data_url(_jpg(img, 320, 80))
                    sess["frames"].append(fr)
                    decision = decision or {"result": "no_people"}
                    continue

                # 프레임 안의 얼굴을 전부 판정
                judged = [self._judge(f, persons, M, owner, m) for f in faces]
                passing: dict[str, dict] = {}
                for j in judged:
                    if j["pass"] and (j["pid"] not in passing or j["sim"] > passing[j["pid"]]["sim"]):
                        passing[j["pid"]] = j
                    if j["sim"] > best_seen["sim"] and j["name"]:
                        best_seen = {"name": j["name"], "sim": j["sim"]}
                for pid in passing:
                    streak[pid] = streak.get(pid, 0) + 1
                    miss[pid] = 0
                if any(j["qok"] for j in judged):
                    # 품질 되는 얼굴이 있는데 이 사람이 통과 못 함 → 불일치 1회 (2회 연속이면 초기화)
                    for pid in list(streak):
                        if pid not in passing:
                            miss[pid] = miss.get(pid, 0) + 1
                            if miss[pid] > 1:
                                streak[pid] = 0

                j0 = judged[0]   # 가장 큰 얼굴 (화면 표시 호환용)
                fr.update({"q": j0["q"], "quality_ok": j0["qok"], "best": j0["name"], "sim": j0["sim"],
                           "second": j0["second"], "thr": j0["thr"], "pass": bool(passing)})
                fr["faces"] = [{"n": k + 1, "best": j["name"], "sim": j["sim"], "thr": j["thr"],
                                "pass": j["pass"], "qok": j["qok"], "size": j["q"]["size"]}
                               for k, j in enumerate(judged)]
                names = {p["id"]: p["name"] for p in persons}
                fr["streaks"] = {names.get(pid, pid): c for pid, c in streak.items() if c}
                fr["streak"] = max(streak.values(), default=0)
                if mode == "test":
                    fr["thumb"] = _data_url(_jpg(_annotate(img, faces, judged), 320, 80))
                sess["frames"].append(fr)

                # 기준 연속 횟수를 채운 사람 → 유사도 높은 순으로 정책 확인
                ready = sorted((j for pid, j in passing.items() if streak[pid] >= j["need"]),
                               key=lambda j: -j["sim"])
                for j in ready:
                    p = j["person"]
                    if not j["strict"]:
                        ok, detail = await self._phone_ok(p)
                        fr["phone"] = detail
                        if not ok:
                            phone_missing = {"person": p["name"], "detail": detail}
                            continue    # 폰이 아직 안 잡힘 → 다른 사람 확인 / 창 안에서 계속 시도
                    decision = {"result": "match", "person": p["name"], "person_id": p["id"],
                                "policy": p.get("policy"), "sim": j["sim"], "streak": streak[p["id"]],
                                "faces_max": faces_max}
                    break
                if decision:
                    break
        finally:
            prod.cancel()
            if watch:
                watch.cancel()
            for t in (prod, watch):
                if t is None:
                    continue
                try:
                    await t
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            self._cancel = False

        if door["opened"]:
            return {"result": "door_opened", "best": best_seen,
                    **({"matched": decision.get("person")} if decision and decision.get("person") else {})}

        if mode == "enroll":
            return {"result": "enroll", "candidates": len(self.candidates)}
        if decision and decision["result"] == "match":
            opened = False
            if mode == "auto" and st["door"]["auto_open"]:
                ent = st["door"]["script"]
                try:
                    # 판정과 동시에 문이 열렸으면 신호를 보내지 않음
                    if sensor:
                        ds = await ha.state(sensor)
                        if ds and ds.get("state") == "on":
                            decision["result"] = "door_opened"
                            decision["opened"] = False
                            return decision
                    # HA는 없는 엔티티 호출도 오류 없이 넘기므로 먼저 존재 확인
                    if not await ha.state(ent):
                        raise RuntimeError(f"{ent} 엔티티가 HA에 없음")
                    await ha.run_script(ent, {"person": decision["person"], "similarity": decision["sim"]})
                    opened = True
                except Exception as e:  # noqa: BLE001
                    decision["open_error"] = str(e)
                    log.error("문 열기 실패: %s", e)
            decision["opened"] = opened
            return decision
        if decision:
            return decision
        if phone_missing:
            return {"result": "phone_missing", **phone_missing, "best": best_seen}
        faces_seen = sum(1 for x in sess["frames"] if x.get("face"))
        return {"result": "no_match" if faces_seen else "no_face", "best": best_seen}

    async def _door_guard(self, st):
        """실제 스캔 시작 전: 문이 열려 있거나 최근에 쓰였으면(나가는 사람) 건너뜀. 문제 없으면 None."""
        sensor = (st["door"].get("sensor") or "").strip()
        if not sensor:
            return None
        stt = await ha.state(sensor)
        if not stt or stt.get("state") in ("unavailable", "unknown"):
            # 문 상태를 모르면 나가는 사람인지 판단할 수 없으므로 안전하게 건너뜀
            return {"result": "door_skip", "detail": f"{sensor} 상태 확인 불가"}
        if stt.get("state") == "on":
            return {"result": "door_skip", "detail": "현관문이 열려 있음"}
        cooldown = int(st["door"].get("cooldown", 180))
        try:
            changed = datetime.fromisoformat(stt["last_changed"]).timestamp()
        except (KeyError, ValueError):
            return None
        ago = time.time() - changed
        if ago < cooldown:
            return {"result": "door_skip", "detail": f"문 사용 {int(ago)}초 전 (기준 {cooldown}초)"}
        return None

    @staticmethod
    def _qok(f, m):
        return (f.quality["size"] >= m["min_face"] and f.quality["yaw"] <= m["max_yaw"]
                and f.quality["pitch"] <= m["max_pitch"])

    def _judge(self, f, persons, M, owner, m):
        """얼굴 하나를 등록된 사람들과 비교해 통과 여부 판정."""
        qok = self._qok(f, m)
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
        return {"person": p, "pid": p["id"], "name": p["name"], "sim": round(bsim, 3),
                "second": round(ssim, 3), "thr": thr, "need": need, "strict": strict,
                "pass": bool(passed), "qok": bool(qok), "q": f.quality}

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
