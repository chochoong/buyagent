"""회귀 테스트 (표준 라이브러리 unittest)

    python -m unittest            # buyagent 폴더에서 실행
    python -m unittest -v         # 항목별 결과 보기

실제 API·웹훅에는 접속하지 않습니다 (로컬 테스트 서버 사용).
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import main as M                                              # noqa: E402
from core import http as H                                    # noqa: E402
from core import report as R                                  # noqa: E402
from core.config import load_config, load_specs               # noqa: E402
from core.models import Offer, Query, Result, Spec            # noqa: E402
from core.rules import blocked_category                       # noqa: E402
from core.storage import Store                                # noqa: E402
from core.validate import ERROR, validate                     # noqa: E402
from sources import REGISTRY                                  # noqa: E402

CFG = load_config(BASE)


def cfg():
    return copy.deepcopy(CFG)


# ---------------------------------------------------------------- 로컬 테스트 서버
class FakeServer:
    """GET: responses 목록을 순서대로 (상태코드, JSON) 반환. POST: 받은 JSON 기록"""

    def __init__(self):
        self.responses: list[tuple[int, dict]] = []
        self.gets: list[tuple[float, str]] = []
        self.posts: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.gets.append((time.monotonic(), self.path))
                code, body = outer.responses.pop(0) if outer.responses else (200, {"ok": 1})
                self.send_response(code)
                if code == 429:
                    self.send_header("Retry-After", "1")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def do_POST(self):
                n = int(self.headers["Content-Length"])
                outer.posts.append(json.loads(self.rfile.read(n)))
                self.send_response(204)
                self.end_headers()

        self.srv = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class TmpDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------- 원가·최저가
class TestPricing(TmpDirTest):
    def setUp(self):
        super().setUp()
        self.run = M.Runner(cfg(), True, 0)
        self.run.store = Store(self.tmp / "t.db")
        self.spec = Spec(id="x", name="x", category="kitchen", keyword="x")
        self.jp = Offer("demo", "jp", "jp", 4000, "JPY", "JP")
        self.us = Offer("demo", "us", "us", 25, "USD", "US")

    def test_cheapest_by_landed_cost(self):
        # 현지가는 US가 싸 보여도 배대지 요금까지 넣으면 JP가 쌈
        self.assertEqual(self.run.cheapest(self.spec, [self.us, self.jp]).offer.key, "jp")

    def test_unknown_currency_mixed(self):
        eur = Offer("demo", "eu", "eu", 1, "EUR", "JP")
        self.assertEqual(self.run.cheapest(self.spec, [eur, self.jp]).offer.key, "jp")
        only = self.run.cheapest(self.spec, [eur])
        self.assertTrue(any("계산 불가" in a for a in only.alerts))


# ---------------------------------------------------------------- 이력·알림
class TestStore(TmpDirTest):
    def setUp(self):
        super().setUp()
        self.st = Store(self.tmp / "h.db")
        self.spec = Spec(id="x", name="x", category="kitchen", keyword="x")

    def test_price_change_in_krw(self):
        jp = Offer("demo", "jp", "jp", 4000, "JPY", "JP")
        us = Offer("demo", "us", "us", 25, "USD", "US")
        self.st.track(Result(spec=self.spec, offer=jp, cost_krw=100000), "t1", 0.03)
        r2 = Result(spec=self.spec, offer=us, cost_krw=101000)
        self.st.track(r2, "t2", 0.03)
        self.assertEqual(r2.events, [], "통화가 바뀌어도 원가 1% 변동은 알림 없음")
        r3 = Result(spec=self.spec, offer=jp, cost_krw=110000)
        self.st.track(r3, "t3", 0.03)
        self.assertTrue(len(r3.events) == 1 and "▲" in r3.events[0])
        r4 = Result(spec=self.spec, offer=None)
        self.st.track(r4, "t4", 0.03)
        self.assertIn("재고 없음", r4.events[0])

    def test_alert_dedup(self):
        a = ["판매처 'A' 정품 확인", "관부가세 개당 약 12,345원"]
        self.assertEqual(self.st.fresh_alerts("p", a), a)
        self.assertEqual(self.st.fresh_alerts("p", ["판매처 'A' 정품 확인", "관부가세 개당 약 12,400원"]), [],
                         "금액만 바뀐 같은 경고는 다시 보내지 않음")
        self.assertEqual(self.st.fresh_alerts("p", ["판매처 'B' 정품 확인"]), ["판매처 'B' 정품 확인"])
        self.st.fresh_alerts("p", ["조회 실패: x"], replace=False)
        self.assertEqual(self.st.fresh_alerts("p", ["판매처 'B' 정품 확인"]), [],
                         "조회 실패 후 복구돼도 기존 경고는 재알림 안 함")


# ---------------------------------------------------------------- 차단 규칙·단어장
class TestRules(unittest.TestCase):
    def test_false_positives_pass(self):
        for name in ["ちいかわ スタイリッシュ トートバッグ", "クリップ ハチワレ", "化粧箱入り 腕時計",
                     "刀剣乱舞 アクリルスタンド", "チョコレートブラウン レザーベルト"]:
            self.assertIsNone(blocked_category(CFG, name), name)

    def test_true_positives_blocked(self):
        for name in ["ベビー用 スタイ", "キャンメイク リップ", "ビタミンC サプリ"]:
            self.assertIsNotNone(blocked_category(CFG, name), name)

    def test_glossary_loaded(self):
        self.assertEqual(CFG["glossary_unknown"], [], "기본 설정에 단어장에 없는 한글이 없어야 함")


# ---------------------------------------------------------------- HTTP·웹훅
class TestHttp(unittest.TestCase):
    def setUp(self):
        self.s = FakeServer()
        H._last_call.clear()

    def tearDown(self):
        self.s.close()

    def test_retry_on_429(self):
        self.s.responses = [(429, {}), (429, {}), (200, {"ok": 1})]
        self.assertEqual(H.get_json(self.s.url + "/a", {"key": "SECRET"}, retries=2), {"ok": 1})
        self.assertEqual(len(self.s.gets), 3)

    def test_error_hides_key(self):
        self.s.responses = [(429, {})]
        with self.assertRaises(RuntimeError) as cm:
            H.get_json(self.s.url + "/a", {"key": "SECRET"}, retries=0)
        self.assertNotIn("SECRET", str(cm.exception))

    def test_min_interval(self):
        t = time.monotonic()
        H.get_json(self.s.url + "/b", min_interval=0.5)
        H.get_json(self.s.url + "/b", min_interval=0.5)
        self.assertGreaterEqual(time.monotonic() - t, 0.5)

    def test_discord_split(self):
        lines = [f"• 줄 {i} " + "가" * 100 for i in range(60)]
        self.assertTrue(R.send_webhook(self.s.url + "/discord", lines))
        txt = "".join(p["content"] for p in self.s.posts)
        self.assertGreater(len(self.s.posts), 1)
        self.assertTrue(all(len(p["content"]) <= 1900 for p in self.s.posts))
        self.assertTrue(all(ln in txt for ln in lines), "잘려서 버려진 줄이 없어야 함")

    def test_test_alert(self):
        c = cfg()
        c["alerts"]["webhook_url"] = self.s.url + "/discord"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(M.test_alert(c), 0)
        self.assertIn("테스트 알림", self.s.posts[0]["content"])
        c["alerts"]["webhook_url"] = ""
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(M.test_alert(c), 1)


# ---------------------------------------------------------------- 소스 플러그인
RAKUTEN_RAW = {"Items": [{"itemName": "セイコー SBTQ041", "itemPrice": 15000, "postageFlag": 0,
                          "availability": 1, "shopName": "A", "itemUrl": "u", "itemCode": "a:1"}]}


class TestSources(unittest.TestCase):
    def setUp(self):
        self.s = FakeServer()
        H._last_call.clear()

    def tearDown(self):
        self.s.close()

    def _rakuten(self, **conf):
        base = {"enabled": True, "application_id": "APP", "endpoint": self.s.url + "/rk",
                "min_interval": 0, "retries": 0}
        return REGISTRY["rakuten"]({**base, **conf}, CFG)

    def test_rakuten_access_key_optional(self):
        src = self._rakuten()
        self.assertTrue(src.ready(), "access_key 없이도 시도는 함")
        self.s.responses = [(200, RAKUTEN_RAW)]
        offers = src.search(Query("x"))
        self.assertEqual(len(offers), 1)
        self.assertNotIn("accessKey", self.s.gets[0][1])
        self._rakuten(access_key="KEY").search(Query("x"))
        self.assertIn("accessKey=KEY", self.s.gets[1][1])

    def test_yahoo_not_used_for_newest(self):
        self.assertFalse(REGISTRY["yahoo"].supports_newest)
        run = M.Runner(cfg(), True, 0)
        run.sources = {"yahoo": REGISTRY["yahoo"]({"enabled": True, "app_id": "x"}, CFG)}
        offers, errors = run.fetch(Spec(id="d", name="d", category="acrylic", keyword="x",
                                        discover=True), newest=True)
        self.assertEqual(offers, [])
        self.assertTrue(any("최신순" in e for e in errors), "야후에 요청하지 않고 이유를 알려줌")
        self.assertEqual(self.s.gets, [])

    def test_probe(self):
        c = cfg()
        c["sources"] = {"rakuten": {"enabled": True, "application_id": "APP", "min_interval": 0,
                                    "retries": 0, "endpoint": self.s.url + "/rk"}}
        self.s.responses = [(200, RAKUTEN_RAW)]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(M.probe(c, [], "テスト"), 0)
        self.assertIn("응답 OK: 원문 1건 → 변환 1건", out.getvalue())
        self.assertIn("itemPrice", out.getvalue())
        self.s.responses = [(400, {"error": "wrong_parameter", "error_description": "accessKey is required"})]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(M.probe(c, [], "テスト"), 1)
        self.assertIn("accessKey is required", out.getvalue(), "API 거절 사유가 그대로 보여야 함")


# ---------------------------------------------------------------- 실행 상태 알림
class TestHealth(TmpDirTest):
    def test_sequence(self):
        from datetime import datetime, timedelta
        f = self.tmp / "health.json"
        t = datetime(2026, 1, 1, 9, 0)
        h = lambda p, dt=0: M.health_messages(p, t + timedelta(hours=dt), f)   # noqa: E731
        self.assertEqual(h(""), [], "정상일 땐 아무 알림 없음")
        self.assertIn("실행 실패", h("감시 5건 모두 조회 실패")[0])
        self.assertEqual(h("감시 6건 모두 조회 실패", 1), [], "숫자만 다른 같은 문제는 재알림 안 함")
        self.assertIn("아직 실패 중", h("감시 6건 모두 조회 실패", 25)[0], "하루 지나면 한 번 더")
        self.assertEqual(h("감시 6건 모두 조회 실패", 26), [])
        self.assertIn("실행 실패", h("프로그램 오류 X", 27)[0], "다른 문제로 바뀌면 바로 알림")
        self.assertIn("정상 복구", h("", 28)[0])
        self.assertEqual(h("", 29), [])


class TestRunFailures(TmpDirTest):
    """프로그램이 조용히 멈추지 않고 실패를 알리는지 (실제 DB·리포트 폴더는 건드리지 않음)"""

    def setUp(self):
        super().setUp()
        self.s = FakeServer()
        H._last_call.clear()
        self.saved = (M.BASE, M.HEALTH_FILE, M.load_config, sys.argv)
        M.BASE, M.HEALTH_FILE = self.tmp, self.tmp / "health.json"

    def tearDown(self):
        M.BASE, M.HEALTH_FILE, M.load_config, sys.argv = self.saved
        self.s.close()
        super().tearDown()

    def _cfg(self, **rakuten):
        c = cfg()
        c["alerts"]["webhook_url"] = self.s.url + "/discord"
        c["sources"] = {"rakuten": {"enabled": True, "min_interval": 0, "retries": 0,
                                    "endpoint": self.s.url + "/rk", **rakuten}}
        return c

    def _main(self, c):
        M.load_config = lambda *a, **k: c
        sys.argv = ["main.py"]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                M.main()
        return cm.exception.code

    def test_no_sources_alerts(self):
        self.assertEqual(self._main(self._cfg()), 1)      # 키 없음
        self.assertIn("사용 가능한 소스가 없습니다", self.s.posts[0]["content"])

    def test_crash_alerts(self):
        c = self._cfg(application_id="APP")
        del c["markets"]                                   # 일부러 망가뜨림 → KeyError
        self.assertEqual(self._main(c), 1)
        self.assertIn("프로그램 오류", self.s.posts[0]["content"])

    def test_all_fetch_failed(self):
        c = self._cfg(application_id="APP")
        self.s.responses = [(401, {"error": "invalid key"})] * 20
        args = type("A", (), {"demo": False, "bundle": 0})()
        specs = [Spec(id="a", name="a", category="watch", keyword="x", must_include=["x"]),
                 Spec(id="b", name="b", category="watch", keyword="y", must_include=["y"])]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            problem = M.run_all(c, args, specs, specs)
        self.assertIn("2건 모두 조회 실패", problem)
        self.assertIn("invalid key", problem)


# ---------------------------------------------------------------- 설정 점검
class TestValidate(unittest.TestCase):
    def _issues(self, c, specs=None):
        specs = load_specs(BASE, c["markets"]) if specs is None else specs
        return validate(c, specs, REGISTRY, BASE)

    def test_current_config_has_no_errors(self):
        self.assertEqual([m for lv, m in self._issues(cfg()) if lv == ERROR], [])

    def test_broken_config_detected(self):
        c = cfg()
        del c["fx"]["manual_krw"]["JPY"]
        del c["shipping_profiles"]["JP"]
        first = next(iter(c["markets"]))
        c["markets"][first]["fee_rate"] = 13
        c["sources"]["ghost"] = {"enabled": True}
        specs = load_specs(BASE, c["markets"])
        specs.append(copy.deepcopy(specs[0]))                       # ID 중복
        specs.append(Spec(id="t", name="t", category="watch", keyword="x", sources=["rakutne"]))
        errs = " / ".join(m for lv, m in self._issues(c, specs) if lv == ERROR)
        for frag in ["JPY", "shipping_profiles.JP", "fee_rate는 소수로", "ghost", "중복", "rakutne"]:
            self.assertIn(frag, errs)

    def test_discover_yahoo_only_warns(self):
        c = cfg()
        c["sources"]["rakuten"]["application_id"] = "x"
        c["sources"]["yahoo"]["app_id"] = "x"
        specs = [Spec(id="discover:a", name="a", category="acrylic", keyword="x",
                      sources=["yahoo"], discover=True)]
        msgs = " / ".join(m for _, m in self._issues(c, specs))
        self.assertIn("최신순 정렬이 없어", msgs)
        self.assertIn("신상 검색이 안 됨", msgs)


if __name__ == "__main__":
    unittest.main()
