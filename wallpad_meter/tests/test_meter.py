"""python3 -m unittest discover -s wallpad_meter/tests  (wallpad_addons 루트에서)"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from app import tariff  # noqa: E402
from app.fetch import check_options, page_urls, remote_script, split_pages  # noqa: E402
from app.parse import parse_days, parse_index, parse_months  # noqa: E402
from app.state import build  # noqa: E402

FX = os.path.join(HERE, "fixtures")
KST = timezone(timedelta(hours=9))
OPTS = {"mango_host": "192.168.0.108", "mango_port": 22, "mango_user": "root",
        "portal_server": "10.0.0.2", "site_code": "TEST", "wallpad_mac": "000000000000",
        "dong": "101", "ho": "1001", "wallpad_ip": "10.9.0.10", "elec_contract": "high"}


def fx(name):
    with open(os.path.join(FX, name), encoding="utf-8") as f:
        return f.read()


class Parse(unittest.TestCase):
    def test_index(self):
        t = parse_index(fx("index.html"))
        self.assertEqual(t, {"elec": 44469.0, "gas": 317.82, "water": 1518.72,
                             "hotwater": 694.04, "heating": 37.39})

    def test_months(self):
        m = parse_months(fx("sub_elec_2026.html"))
        self.assertEqual(m[1], 289.0)
        self.assertEqual(m[11], 287.0)
        self.assertIsNone(m[12])          # 아직 검침 전
        self.assertEqual(len(m), 12)      # 연도 이동 링크(cur_month 없음)는 안 잡힘

    def test_days(self):
        d = parse_days(fx("sub_sub_elec_2026_11.html"))
        self.assertEqual(len(d), 28)
        self.assertEqual(d[0], {"date": "2026-11-01", "total": 44193.0, "usage": 11.0})
        self.assertEqual(d[-1], {"date": "2026-11-28", "total": 44469.0, "usage": 3.0})


class Tariff(unittest.TestCase):
    def test_elec_low_350_matches_published_example(self):
        # 공개 계산 예시: 저압 기타계절 350kWh → 전기요금계 62,690 / 청구 70,640원 (기금 2.7%)
        self.assertEqual(tariff.elec(350, 10, {"elec_contract": "low"}), 70640)

    def test_elec_high_287(self):
        # 고압 기타계절: 기본 1,260 + 200×105 + 87×174 + 287×9 + 287×5 = 41,416
        # 부가세 4,142 + 기금 1,110 → 46,668 → 10원 미만 절사 46,660
        self.assertEqual(tariff.elec(287, 11, {"elec_contract": "high"}), 46660)

    def test_elec_summer_boundary(self):
        # 같은 300kWh라도 하계(7~8월)는 1단계라 기타계절보다 싸야 함
        self.assertLess(tariff.elec(300, 7, {}), tariff.elec(300, 6, {}))

    def test_elec_super_user(self):
        o = {"elec_contract": "low"}
        winter, autumn = tariff.elec(1100, 1, o), tariff.elec(1100, 10, o)
        # 동계 1,000kWh 초과 100kWh는 307.3 → 736.2 (차액 42,890원 + 세금·기금)
        self.assertGreater(winter - autumn, 42890)

    def test_elec_flat(self):
        self.assertEqual(tariff.elec(100, 3, {"elec_flat_price": 150}), 15000)

    def test_gas(self):
        # 50㎥ × 42.7MJ × 22.5268 = 48,094.7 + 1,250 → ×1.1 = 54,279
        self.assertEqual(tariff.gas(50, 11, {}), 54279)

    def test_water(self):
        self.assertEqual(tariff.water(10, 11, {}), 12300)

    def test_heating_season(self):
        # 0.5MWh = 430Mcal, 겨울 115.59 → 49,703.7 ×1.1 = 54,674
        self.assertEqual(tariff.heating(0.5, 1, {}), 54674)
        self.assertLess(tariff.heating(0.5, 7, {}), tariff.heating(0.5, 1, {}))

    def test_none(self):
        for k in tariff.CALC:
            self.assertIsNone(tariff.cost(k, None, 1, {}))


class State(unittest.TestCase):
    def pages(self):
        return {"index": fx("index.html"),
                "months:elec:2026": fx("sub_elec_2026.html"),
                "days:elec": fx("sub_sub_elec_2026_11.html")}

    def test_build(self):
        now = datetime(2026, 11, 28, 12, 30, tzinfo=KST)
        s, failed = build(self.pages(), now, OPTS)
        e = s["items"]["elec"]
        self.assertEqual(s["month"], "2026-11")
        self.assertEqual(e["month"], 287.0)
        self.assertEqual(e["total"], 44469.0)
        self.assertEqual(e["today"], 3.0)
        self.assertEqual(e["yesterday"], 10.0)
        self.assertEqual(e["prev_month"], 324.0)
        # (287-3)/27일 × 30일
        self.assertAlmostEqual(e["projected"], round(284 / 27 * 30, 3))
        self.assertEqual(e["cost"], 46660)
        self.assertEqual(len(e["months"]), 12)
        self.assertEqual(failed, ["가스", "수도", "온수", "난방"])
        self.assertEqual(s["cost_total"], e["cost"])
        json.dumps(s, ensure_ascii=False)

    def test_failed_item_keeps_last(self):
        now = datetime(2026, 11, 28, 12, 30, tzinfo=KST)
        last = {"items": {"gas": {"name": "가스", "month": 12.5, "cost": 15000, "stale": False}}}
        s, failed = build(self.pages(), now, OPTS, last)
        self.assertEqual(s["items"]["gas"]["month"], 12.5)
        self.assertTrue(s["items"]["gas"]["stale"])

    def test_january_prev_month_from_last_year(self):
        now = datetime(2027, 1, 5, 9, 0, tzinfo=KST)
        dec = fx("sub_elec_2026.html").replace("[12월&nbsp;사용량]&nbsp;&nbsp;&nbsp;&nbsp;",
                                               "[12월&nbsp;사용량]&nbsp;&nbsp;&nbsp;301.000&nbsp;")
        pages = {"months:elec:2027": "", "months:elec:2026": dec,
                 "days:elec": "<h3>2027-01-01(44800.000/9.000)</h3><h3>2027-01-02(44809.000/10.000)</h3>"}
        s, _ = build(pages, now, OPTS)
        e = s["items"]["elec"]
        self.assertEqual(e["prev_month"], 301.0)
        self.assertEqual(e["month"], 19.0)       # 월별 칸이 비면 일별 합으로 대신


class Fetch(unittest.TestCase):
    def test_options_reject_injection(self):
        self.assertEqual(check_options(OPTS), [])
        self.assertIn("dong", check_options({**OPTS, "dong": "101'; rm -rf /"}))
        self.assertIn("mango_host", check_options({**OPTS, "mango_host": ""}))

    def test_urls(self):
        u = page_urls(OPTS, 2026, 11, ["elec"])
        self.assertEqual(set(u), {"index", "months:elec:2026", "days:elec"})
        self.assertIn("cur_month=11&item=elec", u["days:elec"])
        self.assertIn("months:elec:2025", page_urls(OPTS, 2026, 1, ["elec"]))

    def test_split(self):
        out = split_pages("junk\r\n@@B index\r\n<a>\r\nb\r\n@@E index\r\n@@B x\r\n@@DONE\r\n")
        self.assertEqual(out, {"index": "<a>\nb"})

    def test_remote_script_quoting_end_to_end(self):
        """망고 sh -s → 가짜 adb → 월패드 sh → 가짜 busybox wget 까지 실제 셸로 돌려 & 따옴표 처리 확인."""
        files = {"index.php": "index.html", "sub.php": "sub_elec_2026.html",
                 "sub_sub.php": "sub_sub_elec_2026_11.html"}
        with tempfile.TemporaryDirectory() as d:
            # adb shell '<cmd>' → 월패드 쪽 셸이 cmd를 해석 (실제 adb처럼 CRLF로 출력).
            # 진짜 adb처럼 표준입력을 먼저 읽어 버림 — 스크립트 나머지를 삼키면 페이지가 빠져 실패한다.
            self._exe(d, "adb", '#!/bin/sh\n[ "$1" = shell ] || exit 1\nshift\ncat >/dev/null\n'
                                'sh -c "$*" | sed "s/$/\\r/"\n')
            # busybox wget -qO- URL → URL 그대로 기록 + 파일명에 맞는 fixture 출력
            cases = "\n".join(f'  *"/{k}?"*) cat "{FX}/{v}";;' for k, v in files.items())
            self._exe(d, "busybox", '#!/bin/sh\n[ "$1" = wget ] || exit 1\nurl="$3"\n'
                                    f'echo "$url" >> "{d}/urls.log"\ncase "$url" in\n{cases}\nesac\n')
            pages = page_urls(OPTS, 2026, 11, ["elec"])
            env = {**os.environ, "PATH": d + os.pathsep + os.environ["PATH"]}

            # bash는 파이프로 받은 스크립트를 한 바이트씩 읽는다 → adb가 표준입력을 삼키면
            # 남은 줄이 사라지는 최악 조건(망고 busybox ash도 작은 단위로 읽음)을 재현
            shell = shutil.which("bash") or "sh"

            async def run():
                p = await asyncio.create_subprocess_exec(
                    shell, "-s", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, env=env)
                out, _ = await p.communicate(remote_script(pages).encode())
                return out.decode()

            out = asyncio.run(run())
            with open(os.path.join(d, "urls.log")) as f:
                urls = f.read().split()
        self.assertEqual(urls, list(pages.values()))     # &가 셸에 안 잘리고 URL 그대로 도착
        got = split_pages(out)
        self.assertEqual(parse_index(got["index"])["elec"], 44469.0)
        self.assertEqual(parse_months(got["months:elec:2026"])[11], 287.0)
        self.assertEqual(len(parse_days(got["days:elec"])), 28)

    @staticmethod
    def _exe(d, name, body):
        path = os.path.join(d, name)
        with open(path, "w") as f:
            f.write(body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)


class Discovery(unittest.TestCase):
    def test_templates_render(self):
        try:
            import jinja2
        except ImportError:
            self.skipTest("jinja2 없음")
        os.environ.setdefault("METER_DATA", tempfile.mkdtemp())
        from app.main import discovery
        now = datetime(2026, 11, 28, 12, 30, tzinfo=KST)
        s, _ = build(State().pages(), now, OPTS)
        env = jinja2.Environment()
        for path, cfg in discovery().items():
            if cfg.get("state_topic") != "wallpad_meter/state":
                continue      # 상태 센서는 health 토픽을 읽음
            for k in ("value_template", "json_attributes_template"):
                if k in cfg:
                    r = env.from_string(cfg[k]).render(value_json=json.loads(json.dumps(s)))
                    self.assertTrue(r, f"{path} {k}")
        r = env.from_string(discovery()["sensor/wallpad_meter/gas_month"]["value_template"]
                            ).render(value_json=s)
        self.assertEqual(r, "None")       # 없는 항목 → HA에선 '알 수 없음'
        r = env.from_string(discovery()["sensor/wallpad_meter/elec_cost"]["value_template"]
                            ).render(value_json=s)
        self.assertEqual(r, "46660")


if __name__ == "__main__":
    unittest.main()
