"""설정·사람·기록 저장 (/data 아래 JSON)."""
from __future__ import annotations

import copy
import json
import os
import threading
import time
import uuid

import numpy as np

DEFAULT_SETTINGS = {
    "mango": {"host": "", "port": 22, "user": "root"},
    "capture": {"warmup": 2, "window_sec": 12, "max_frames": 30},
    "match": {
        "thr_phone": 0.50,      # 얼굴+폰 정책 유사도 기준
        "consec_phone": 2,
        "thr_strict": 0.62,     # 얼굴 단독(엄격) 정책
        "consec_strict": 4,
        "min_margin": 0.08,     # 1등-2등 사람 유사도 차이
        "min_face": 70,         # 얼굴 최소 크기(px)
        "max_yaw": 0.35,        # 정면도 한계(0=정면)
        "max_pitch": 0.35,
        "det_size": 320,        # 검출 입력 크기(작을수록 빠름, 문 앞 얼굴엔 320 충분)
    },
    "door": {"auto_open": False, "script": "script.doorface_open",
             "sensor": "",          # 현관문 열림센서(binary_sensor). 비우면 문 검사 안 함
             "cooldown": 180,       # 문이 열리고 닫힌 뒤 이 시간(초) 동안은 실제 스캔 건너뜀(나가는 사람)
             "on_unknown": "skip"}, # 센서 상태 모름: skip=건너뜀 / phone_only=얼굴+폰 정책만 허용하고 스캔
    "tuning_save": False,
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        elif k in out:
            out[k] = v
    return out


class JsonFile:
    def __init__(self, path, default):
        self.path, self.lock = path, threading.RLock()
        try:
            with open(path) as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = copy.deepcopy(default)

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)


class Settings(JsonFile):
    def __init__(self, data_dir):
        super().__init__(os.path.join(data_dir, "settings.json"), DEFAULT_SETTINGS)
        self.data = _merge(DEFAULT_SETTINGS, self.data)

    def get(self) -> dict:
        return copy.deepcopy(self.data)

    def update(self, patch: dict) -> dict:
        with self.lock:
            self.data = _merge(self.data, patch)
            self.save()
            return self.get()


class People(JsonFile):
    """사람: {id,name,policy('phone'|'strict'),phone_entity,phone_states,enabled,samples:[{id,file,q,emb}]}"""

    def __init__(self, data_dir):
        self.dir = os.path.join(data_dir, "faces")
        os.makedirs(self.dir, exist_ok=True)
        super().__init__(os.path.join(data_dir, "people.json"), {"model": "", "people": []})
        self._cache = None

    def list(self, with_emb=False):
        out = []
        for p in self.data["people"]:
            q = {k: v for k, v in p.items() if k != "samples"}
            q["samples"] = [{k: v for k, v in s.items() if with_emb or k != "emb"} for s in p["samples"]]
            out.append(q)
        return out

    def get(self, pid):
        return next((p for p in self.data["people"] if p["id"] == pid), None)

    def add(self, name, policy="phone"):
        with self.lock:
            p = {"id": uuid.uuid4().hex[:8], "name": name.strip() or "이름없음", "policy": policy,
                 "phone_entity": "", "phone_states": "home", "enabled": True, "samples": []}
            self.data["people"].append(p)
            self.save()
            self._cache = None
            return p

    def update(self, pid, patch):
        with self.lock:
            p = self.get(pid)
            if not p:
                return None
            for k in ("name", "policy", "phone_entity", "phone_states", "enabled"):
                if k in patch:
                    p[k] = patch[k]
            self.save()
            self._cache = None
            return p

    def delete(self, pid):
        with self.lock:
            p = self.get(pid)
            if not p:
                return False
            for s in p["samples"]:
                try:
                    os.remove(os.path.join(self.dir, s["file"]))
                except OSError:
                    pass
            self.data["people"].remove(p)
            self.save()
            self._cache = None
            return True

    def add_sample(self, pid, jpg_bytes: bytes, emb: np.ndarray, q: dict):
        with self.lock:
            p = self.get(pid)
            if not p:
                return None
            sid = uuid.uuid4().hex[:10]
            fn = f"{pid}_{sid}.jpg"
            with open(os.path.join(self.dir, fn), "wb") as f:
                f.write(jpg_bytes)
            s = {"id": sid, "file": fn, "q": q, "emb": [round(float(x), 6) for x in emb], "ts": time.time()}
            p["samples"].append(s)
            self.save()
            self._cache = None
            return s

    def delete_sample(self, pid, sid):
        with self.lock:
            p = self.get(pid)
            if not p:
                return False
            s = next((x for x in p["samples"] if x["id"] == sid), None)
            if not s:
                return False
            try:
                os.remove(os.path.join(self.dir, s["file"]))
            except OSError:
                pass
            p["samples"].remove(s)
            self.save()
            self._cache = None
            return True

    def gallery(self):
        """(person 목록, 임베딩 행렬, 행→사람 인덱스) — 활성 사람만."""
        with self.lock:
            if self._cache is None:
                persons, embs, owner = [], [], []
                for p in self.data["people"]:
                    if not p.get("enabled", True) or not p["samples"]:
                        continue
                    persons.append(p)
                    for s in p["samples"]:
                        embs.append(s["emb"])
                        owner.append(len(persons) - 1)
                M = np.array(embs, dtype=np.float32) if embs else np.zeros((0, 512), np.float32)
                self._cache = (persons, M, np.array(owner, dtype=np.int32))
            return self._cache

    def reembed(self, model_name, embed_fn):
        """모델이 바뀌면 저장된 정렬 얼굴(112x112)로 임베딩을 다시 계산."""
        with self.lock:
            if self.data.get("model") == model_name:
                return 0
            n = 0
            for p in self.data["people"]:
                for s in p["samples"]:
                    path = os.path.join(self.dir, s["file"])
                    if os.path.exists(path):
                        s["emb"] = [round(float(x), 6) for x in embed_fn(path)]
                        n += 1
            self.data["model"] = model_name
            self.save()
            self._cache = None
            return n


class Events:
    """기록(사진 없음). 최근 500건 유지."""

    def __init__(self, data_dir, keep=500):
        self.path, self.keep, self.lock = os.path.join(data_dir, "events.jsonl"), keep, threading.Lock()

    def add(self, ev: dict):
        ev = {"ts": time.time(), **ev}
        with self.lock:
            lines = []
            if os.path.exists(self.path):
                with open(self.path) as f:
                    lines = f.readlines()[-(self.keep - 1):]
            lines.append(json.dumps(ev, ensure_ascii=False) + "\n")
            with open(self.path, "w") as f:
                f.writelines(lines)
        return ev

    def recent(self, n=100):
        if not os.path.exists(self.path):
            return []
        with open(self.path) as f:
            lines = f.readlines()[-n:]
        return [json.loads(x) for x in reversed(lines)]
