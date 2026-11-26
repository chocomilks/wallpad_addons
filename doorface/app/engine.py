"""얼굴 검출(SCRFD) + 정렬 + 임베딩(ArcFace) — insightface 패키지 없이 onnxruntime만 사용."""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass

import cv2
import numpy as np
import onnxruntime as ort

# ArcFace 표준 5점 정렬 좌표 (112x112)
ARCFACE_DST = np.array(
    [[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
     [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float32)


@dataclass
class Face:
    bbox: np.ndarray        # x1,y1,x2,y2
    kps: np.ndarray         # 5x2 (왼눈, 오른눈, 코, 입왼, 입오)
    det_score: float
    embedding: np.ndarray | None = None
    aligned: np.ndarray | None = None   # 112x112 BGR
    quality: dict | None = None

    @property
    def size(self) -> int:
        return int(min(self.bbox[2] - self.bbox[0], self.bbox[3] - self.bbox[1]))


def _session(path: str) -> ort.InferenceSession:
    so = ort.SessionOptions()
    so.intra_op_num_threads = max(1, (os.cpu_count() or 2) - 1)
    so.log_severity_level = 3
    return ort.InferenceSession(path, so, providers=["CPUExecutionProvider"])


class SCRFD:
    def __init__(self, path: str, input_size=(640, 640)):
        self.sess = _session(path)
        self.input_name = self.sess.get_inputs()[0].name
        self.input_size = input_size
        outs = self.sess.get_outputs()
        n = len(outs)
        # det_10g / det_500m: 9 outputs (score,bbox,kps × 3 strides)
        if n == 9:
            self.fmc, self.strides, self.use_kps = 3, [8, 16, 32], True
        elif n == 6:
            self.fmc, self.strides, self.use_kps = 3, [8, 16, 32], False
        else:
            raise RuntimeError(f"지원하지 않는 SCRFD 출력 수: {n}")
        self.num_anchors = 2
        self._centers: dict = {}

    def _anchor_centers(self, h, w, stride):
        key = (h, w, stride)
        if key not in self._centers:
            c = np.stack(np.mgrid[:h, :w][::-1], axis=-1).astype(np.float32)
            c = (c * stride).reshape(-1, 2)
            c = np.stack([c] * self.num_anchors, axis=1).reshape(-1, 2)
            self._centers[key] = c
        return self._centers[key]

    def detect(self, img: np.ndarray, thresh=0.5, max_num=0) -> list[Face]:
        ih, iw = img.shape[:2]
        W, H = self.input_size
        ratio = min(W / iw, H / ih)
        nw, nh = int(iw * ratio), int(ih * ratio)
        det_img = np.zeros((H, W, 3), dtype=np.uint8)
        det_img[:nh, :nw] = cv2.resize(img, (nw, nh))
        blob = cv2.dnn.blobFromImage(det_img, 1.0 / 128, (W, H), (127.5, 127.5, 127.5), swapRB=True)
        outs = self.sess.run(None, {self.input_name: blob})
        scores_l, boxes_l, kps_l = [], [], []
        for i, s in enumerate(self.strides):
            scores = outs[i].reshape(-1)
            bbox = outs[i + self.fmc].reshape(-1, 4) * s
            centers = self._anchor_centers(H // s, W // s, s)
            keep = np.where(scores >= thresh)[0]
            if not len(keep):
                continue
            c = centers[keep]
            b = bbox[keep]
            boxes = np.stack([c[:, 0] - b[:, 0], c[:, 1] - b[:, 1], c[:, 0] + b[:, 2], c[:, 1] + b[:, 3]], axis=-1)
            scores_l.append(scores[keep])
            boxes_l.append(boxes)
            if self.use_kps:
                k = outs[i + self.fmc * 2].reshape(-1, 10)[keep] * s
                k = k.reshape(-1, 5, 2) + c[:, None, :]
                kps_l.append(k)
        if not scores_l:
            return []
        scores = np.concatenate(scores_l)
        boxes = np.concatenate(boxes_l) / ratio
        kpss = np.concatenate(kps_l) / ratio if kps_l else None
        idx = cv2.dnn.NMSBoxes(
            [[float(x1), float(y1), float(x2 - x1), float(y2 - y1)] for x1, y1, x2, y2 in boxes],
            scores.tolist(), thresh, 0.4)
        idx = np.array(idx).reshape(-1)
        faces = [Face(boxes[j], kpss[j] if kpss is not None else None, float(scores[j])) for j in idx]
        faces.sort(key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]), reverse=True)
        return faces[:max_num] if max_num else faces


class ArcFace:
    def __init__(self, path: str):
        self.sess = _session(path)
        self.input_name = self.sess.get_inputs()[0].name

    def embed(self, aligned_bgr: list[np.ndarray]) -> np.ndarray:
        blob = cv2.dnn.blobFromImages(aligned_bgr, 1.0 / 127.5, (112, 112), (127.5, 127.5, 127.5), swapRB=True)
        e = self.sess.run(None, {self.input_name: blob})[0]
        return e / np.linalg.norm(e, axis=1, keepdims=True)


def align(img: np.ndarray, kps: np.ndarray) -> np.ndarray:
    M, _ = cv2.estimateAffinePartial2D(kps.astype(np.float32), ARCFACE_DST, method=cv2.LMEDS)
    return cv2.warpAffine(img, M, (112, 112), borderValue=0.0)


def quality(img: np.ndarray, f: Face) -> dict:
    """정면도·선명도·크기. yaw/pitch는 5점 기반 근사값(0=정면, 1≈심한 측면)."""
    le, re, nose, ml, mr = f.kps
    eye_mid = (le + re) / 2
    eye_dist = max(np.linalg.norm(re - le), 1e-3)
    # 코가 두 눈 중앙에서 좌우로 얼마나 벗어났나 → yaw
    yaw = float(abs(np.dot(nose - eye_mid, (re - le) / eye_dist)) / eye_dist)
    mouth_mid = (ml + mr) / 2
    em = max(np.linalg.norm(mouth_mid - eye_mid), 1e-3)
    # 코가 눈-입 사이 어디쯤인가 (정면이면 ~0.5 부근) → pitch 근사
    t = float(np.dot(nose - eye_mid, mouth_mid - eye_mid) / em ** 2)
    pitch = abs(t - 0.55)
    gray = cv2.cvtColor(f.aligned, cv2.COLOR_BGR2GRAY) if f.aligned is not None else None
    sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var()) if gray is not None else 0.0
    return {"size": f.size, "det": round(f.det_score, 3), "yaw": round(yaw, 3),
            "pitch": round(pitch, 3), "sharp": round(sharp, 1)}


class Engine:
    def __init__(self, model_dir: str, det_name="det_10g.onnx", rec_name="w600k_r50.onnx"):
        self.det = SCRFD(os.path.join(model_dir, det_name))
        self.rec = ArcFace(os.path.join(model_dir, rec_name))
        self.lock = threading.Lock()

    def analyze(self, img: np.ndarray, det_thresh=0.5, max_faces=1) -> list[Face]:
        with self.lock:
            faces = self.det.detect(img, det_thresh, max_faces)
            if not faces:
                return []
            for f in faces:
                f.aligned = align(img, f.kps)
            embs = self.rec.embed([f.aligned for f in faces])
            for f, e in zip(faces, embs):
                f.embedding = e.astype(np.float32)
                f.quality = quality(img, f)
            return faces
