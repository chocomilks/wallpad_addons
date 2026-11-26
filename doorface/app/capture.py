"""도어카메라 프레임 캡처: 애드온 → SSH(망고) → adb → 월패드 cmxmm videoCapture.

한 SSH 세션 안에서 #201(세대현관 모니터링) 호출 후 프레임을 연속으로 캡처해
base64 텍스트로 흘려보내고, 여기서 한 장씩 디코딩해 넘긴다.
"""
from __future__ import annotations

import asyncio
import base64
import glob
import logging
import os
from typing import AsyncIterator

log = logging.getLogger("doorface.capture")

CMXMM_PORT = 29720
REMOTE_FILE = "/tmp/doorface.jpg"


def soap_request_b64(w=720, h=480, path=REMOTE_FILE) -> str:
    body = (
        '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:ns1="urn:cmm"><SOAP-ENV:Body><ns1:videoCapture>'
        f"<capw>{w}</capw><caph>{h}</caph><file>{path}</file>"
        "</ns1:videoCapture></SOAP-ENV:Body></SOAP-ENV:Envelope>"
    )
    req = (
        "POST / HTTP/1.1\r\nContent-Type: text/xml\r\n"
        f"Content-Length: {len(body.encode())}\r\nConnection: close\r\n\r\n{body}"
    )
    return base64.b64encode(req.encode()).decode()


def remote_script(frames: int, warmup: int, call: bool) -> str:
    req = soap_request_b64()
    lines = [f"REQ='{req}'"]
    if call:
        lines.append(
            "adb shell 'am broadcast -a com.commax.sipua.NEW_OUTGOING_CALL "
            "--es android.intent.extra.PHONE_NUMBER \"#201\"' >/dev/null 2>&1")
        lines.append(f"sleep {int(warmup)}")
    lines += [
        "i=0",
        f"while [ $i -lt {int(frames)} ]; do",
        '  echo "@@F $i"',
        f'  adb shell "echo $REQ | busybox base64 -d | busybox nc -w 5 127.0.0.1 {CMXMM_PORT} >/dev/null; '
        f'busybox base64 {REMOTE_FILE} 2>/dev/null; busybox rm -f {REMOTE_FILE}"',
        '  echo "@@E $i"',
        "  i=$((i+1))",
        "done",
        'echo "@@DONE"',
    ]
    return "\n".join(lines) + "\n"


class Capturer:
    def __init__(self, data_dir: str, settings):
        self.ssh_dir = os.path.join(data_dir, "ssh")
        self.key = os.path.join(self.ssh_dir, "id_ed25519")
        self.settings = settings
        self.mock_dir = os.environ.get("DOORFACE_MOCK_DIR")

    # ---------- SSH 키 ----------
    async def ensure_key(self):
        os.makedirs(self.ssh_dir, exist_ok=True)
        if not os.path.exists(self.key):
            try:
                p = await asyncio.create_subprocess_exec(
                    "ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "doorface-addon", "-f", self.key)
                await p.wait()
            except FileNotFoundError:
                log.error("ssh-keygen 없음 — openssh-client 필요")

    def public_key(self) -> str:
        try:
            with open(self.key + ".pub") as f:
                return f.read().strip()
        except OSError:
            return ""

    def _ssh_cmd(self) -> list[str]:
        m = self.settings.get()["mango"]
        return [
            "ssh", "-i", self.key, "-p", str(m["port"]),
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"UserKnownHostsFile={os.path.join(self.ssh_dir, 'known_hosts')}",
            "-o", "ServerAliveInterval=5",
            f"{m['user']}@{m['host']}", "sh", "-s",
        ]

    async def test(self) -> dict:
        if self.mock_dir:
            return {"ok": True, "detail": "mock mode"}
        if not self.settings.get()["mango"]["host"]:
            return {"ok": False, "detail": "망고 주소가 비어 있음"}
        p = await asyncio.create_subprocess_exec(
            *self._ssh_cmd(), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(
                p.communicate(b"adb devices; adb shell 'busybox pidof cmxmm'\n"), 20)
        except asyncio.TimeoutError:
            p.kill()
            return {"ok": False, "detail": "시간 초과"}
        text = out.decode(errors="replace").replace("\r", "")
        ok = p.returncode == 0 and "\tdevice" in text
        return {"ok": ok, "detail": text.strip()[-800:]}

    # ---------- 프레임 스트림 ----------
    async def frames(self, n: int, warmup: int, call: bool = True) -> AsyncIterator[bytes]:
        if self.mock_dir:
            async for b in self._mock(n, warmup):
                yield b
            return
        p = await asyncio.create_subprocess_exec(
            *self._ssh_cmd(), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=4 * 1024 * 1024)
        p.stdin.write(remote_script(n, warmup, call).encode())
        await p.stdin.drain()
        p.stdin.close()
        buf: list[str] | None = None
        try:
            while True:
                line = await asyncio.wait_for(p.stdout.readline(), warmup + 20)
                if not line:
                    break
                s = line.decode(errors="replace").strip()
                if s.startswith("@@F"):
                    buf = []
                elif s.startswith("@@E"):
                    data = b""
                    try:
                        data = base64.b64decode("".join(buf or []), validate=False)
                    except Exception:  # noqa: BLE001
                        pass
                    buf = None
                    if data[:2] == b"\xff\xd8":
                        yield data
                    else:
                        log.debug("빈 프레임(캡처 실패)")
                elif s.startswith("@@DONE"):
                    break
                elif buf is not None and s:
                    buf.append(s)
        finally:
            if p.returncode is None:
                p.kill()
            err = b""
            try:
                err = await asyncio.wait_for(p.stderr.read(), 2)
            except Exception:  # noqa: BLE001
                pass
            await p.wait()
            if err.strip():
                log.info("ssh: %s", err.decode(errors="replace").strip()[-300:])

    async def _mock(self, n, warmup):
        files = sorted(glob.glob(os.path.join(self.mock_dir, "*.jp*g")))
        await asyncio.sleep(min(warmup, 1))
        for i in range(n):
            if not files:
                return
            await asyncio.sleep(0.35)
            with open(files[i % len(files)], "rb") as f:
                yield f.read()
