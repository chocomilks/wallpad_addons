"""포털 페이지 가져오기: 애드온 → SSH(망고) → adb → 월패드 busybox wget → 단지서버.

단지서버(10.0.0.2)는 월패드만 닿으므로, doorface처럼 망고에 SSH로 짧은 스크립트를
표준입력으로 넘겨 실행한다. 망고에는 파일을 남기지 않고, 페이지 HTML은
@@B 키 / @@E 키 표식 사이로 흘려보낸다. 한 번에 ~11페이지(각 수 KB), 몇 초면 끝난다.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re

log = logging.getLogger("wallpad_meter.fetch")

_SAFE = re.compile(r"^[A-Za-z0-9._-]+$")
PARAMS = ("site_code", "wallpad_mac", "dong", "ho", "wallpad_ip")


def check_options(o: dict) -> list[str]:
    """비었거나 이상한 옵션 이름 목록 (URL·셸에 그대로 들어가므로 안전한 글자만 허용)."""
    bad = [k for k in ("mango_host", "portal_server", *PARAMS) if not _SAFE.match(str(o.get(k, "")))]
    if not _SAFE.match(str(o.get("mango_user", ""))):
        bad.append("mango_user")
    return bad


def page_urls(o: dict, year: int, month: int, items: list[str]) -> dict[str, str]:
    base = f"http://{o['portal_server']}/m/remote/"
    q = (f"code={o['site_code']}&mac={o['wallpad_mac']}&dong={o['dong']}&ho={o['ho']}"
         f"&wpip={o['wallpad_ip']}&deip={o['wallpad_ip']}")
    pages = {"index": f"{base}index.php?{q}"}
    for k in items:
        pages[f"months:{k}:{year}"] = f"{base}sub.php?{q}&cur_year={year}&item={k}"
        pages[f"days:{k}"] = f"{base}sub_sub.php?{q}&cur_year={year}&cur_month={month}&item={k}"
        if month == 1:  # 지난달(12월)은 작년 페이지에 있음
            pages[f"months:{k}:{year - 1}"] = f"{base}sub.php?{q}&cur_year={year - 1}&item={k}"
    return pages


def remote_script(pages: dict[str, str]) -> str:
    lines = []
    for key, url in pages.items():
        # 월패드 셸에선 큰따옴표가 &를 지키고, 망고 셸에선 작은따옴표가 전체를 지킨다.
        # </dev/null: adb가 표준입력(= 아직 안 읽힌 나머지 스크립트)을 삼키지 않게.
        # 포털 HTML은 줄바꿈 없이 </html>로 끝나므로 표식 앞에 빈 줄을 하나 넣는다.
        lines += [f'echo "@@B {key}"',
                  f"adb shell 'busybox wget -qO- \"{url}\"' </dev/null 2>/dev/null",
                  "echo",
                  f'echo "@@E {key}"']
    lines.append('echo "@@DONE"')
    return "\n".join(lines) + "\n"


def split_pages(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    key, buf = None, []
    for raw in text.replace("\r", "").split("\n"):
        if raw.startswith("@@B "):
            key, buf = raw[4:].strip(), []
        elif raw.startswith("@@E "):
            if key is not None:
                out[key] = "\n".join(buf)
            key = None
        elif key is not None:
            buf.append(raw)
    return out


class Fetcher:
    def __init__(self, data_dir: str):
        self.ssh_dir = os.path.join(data_dir, "ssh")
        self.key = os.path.join(self.ssh_dir, "id_ed25519")

    async def ensure_key(self) -> str:
        os.makedirs(self.ssh_dir, exist_ok=True)
        if not os.path.exists(self.key):
            p = await asyncio.create_subprocess_exec(
                "ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "wallpad-meter-addon", "-f", self.key)
            await p.wait()
        try:
            with open(self.key + ".pub") as f:
                return f.read().strip()
        except OSError:
            return ""

    def _ssh(self, o: dict) -> list[str]:
        return ["ssh", "-i", self.key, "-p", str(o.get("mango_port", 22)),
                "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", f"UserKnownHostsFile={os.path.join(self.ssh_dir, 'known_hosts')}",
                "-o", "ServerAliveInterval=10",
                f"{o['mango_user']}@{o['mango_host']}", "sh", "-s"]

    async def fetch(self, o: dict, pages: dict[str, str], timeout: float = 180) -> dict[str, str]:
        p = await asyncio.create_subprocess_exec(
            *self._ssh(o), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(p.communicate(remote_script(pages).encode()), timeout)
        except asyncio.TimeoutError:
            p.kill()
            await p.wait()
            raise RuntimeError(f"망고 SSH 시간 초과({int(timeout)}초)")
        text = out.decode("utf-8", errors="replace")
        if "@@DONE" not in text:
            msg = err.decode(errors="replace").strip()[-300:] or f"종료코드 {p.returncode}"
            raise RuntimeError(f"망고 SSH 실패: {msg}")
        return split_pages(text)
