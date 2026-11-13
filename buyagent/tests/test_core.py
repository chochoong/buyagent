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
        self.post_responses: list[dict] = []
        self.post_paths: list[str] = []
        self.post_headers: list[dict] = []
        self.headers: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.gets.append((time.monotonic(), self.path))
                outer.headers.append(dict(self.headers))
                code, body = outer.responses.pop(0) if outer.responses else (200, {"ok": 1})
                self.send_response(code)
                if code == 429:
                    self.send_header("Retry-After", "1")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def do_POST(self):
                n = int(self.headers["Content-Length"])
                outer.post_paths.append(self.path)
                outer.post_headers.append(dict(self.headers))
                outer.posts.append(json.loads(self.rfile.read(n)))
                if outer.post_responses:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(json.dumps(outer.post_responses.pop(0)).encode())
                else:
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


# ---------------------------------------------------------------- 중국: 키보드 구매대행 (수동 가격)
class TestKeyboardManual(TmpDirTest):
    def test_keyboard_ignores_electronics_block(self):
        self.assertIsNone(blocked_category(CFG, "Bluetooth ワイヤレス キーボード", "keyboard"))
        self.assertEqual(blocked_category(CFG, "Bluetooth ワイヤレス キーボード"), "electronics",
                         "다른 카테고리에서는 그대로 차단")

    def _src(self, rows: str):
        f = self.tmp / "manual.csv"
        f.write_text("id,price,currency,origin,shipping,in_stock,shop,url,checked\n" + rows, encoding="utf-8")
        return REGISTRY["manual"]({"enabled": True, "file": str(f), "stale_days": 7}, CFG)

    def test_manual_source(self):
        from datetime import date, timedelta
        today = date.today().isoformat()
        old = (date.today() - timedelta(days=30)).isoformat()
        src = self._src(f"kb,299,CNY,CN,0,Y,A,,{today}\nkb,280,CNY,CN,10,N,B,,{old}\nother,1,CNY,CN,0,Y,C,,{today}\n")
        offers = src.search(Query("무선 키보드", spec_id="kb"))
        self.assertEqual([o.shop for o in offers], ["A", "B"], "id가 같은 줄만")
        self.assertTrue(offers[0].in_stock and not offers[1].in_stock)
        self.assertEqual(offers[0].notes, [])
        self.assertIn("30일 전", offers[1].notes[0], "오래된 가격은 재확인 알림")
        self.assertEqual(src.search(Query("x", spec_id="none")), [])

    def test_keyboard_cost_and_alerts(self):
        c = cfg()
        c["fx"]["auto"] = False
        run = M.Runner(c, False, 0)
        run.store = Store(self.tmp / "t.db")
        run.sources = {"manual": self._src(f"kb,299,CNY,CN,0,Y,A,,{__import__('datetime').date.today()}\n")}
        spec = Spec(id="kb", name="75% 무선 키보드", category="keyboard", keyword="75% 무선 키보드",
                    sources=["manual"])
        r = run.run_product(spec)
        # 299위안 × 195 × 1.015 + 배대지 medium 9,000 + 포장 1,000
        self.assertEqual(r.cost_krw, round(299 * 195 * 1.015 + 9000 + 1000))
        self.assertTrue(any("전파법" in a for a in r.alerts), "무선 → 전파법 경고")
        self.assertIn("1대", r.notice)


# ---------------------------------------------------------------- 중국: 사입 계산
from core.sourcing import Item, calculate, parse_size   # noqa: E402

SC = {
    "purchase": {"agent_fee_rate": 0.05, "card_fx_fee_rate": 0.015},
    "freight": {"method": "sea", "sea": {"krw_per_kg": 2500, "krw_per_cbm": 150000, "min_krw": 30000},
                "air": {"krw_per_kg": 7000, "volumetric_divisor": 6000, "min_krw": 15000}},
    "customs": {"vat_rate": 0.10, "broker_fee_krw": 33000, "other_fees_krw": 0},
    "sale": {"inbound_krw": 10000, "packaging_krw": 500, "outbound_shipping_krw": 3000},
    "tax": {"type": "general"},
}


class TestSourcing(unittest.TestCase):
    def setUp(self):
        self.c = cfg()
        self.c["fx"]["auto"] = False
        from core.fx import FX
        self.fx = FX(self.c, True)

    def keycap(self, **kw):
        base = dict(id="k", name="키캡", category="keycap", unit_price=38, qty=50, cn_shipping=20,
                    weight_kg=0.6, size_cm=(33, 13, 5))
        return Item(**{**base, **kw})

    def test_parse_size(self):
        self.assertEqual(parse_size("33x13x5"), (33, 13, 5))
        self.assertEqual(parse_size("33 × 13 × 5"), (33, 13, 5))
        self.assertIsNone(parse_size("33x13"))

    def test_keycap_cost_by_hand(self):
        ln = calculate(self.c, SC, self.fx, [self.keycap()])[0]
        goods = (38 * 50 + 20) * 195                  # 374,400원
        self.assertEqual(ln.goods_krw, round(goods * 1.015))
        self.assertEqual(ln.freight_krw, 75000)       # 30kg × 2,500 (부피 0.107㎥ × 15만보다 큼)
        self.assertEqual(ln.duty_krw, 0)               # 키캡 관세 0%
        self.assertEqual(ln.vat_krw, round((goods + 75000) * 0.1))
        landed = ln.goods_krw + ln.agent_fee_krw + 75000 + 43000   # 일반과세자: 수입 부가세는 환급 → 제외
        self.assertEqual(ln.unit_cost, round(landed / 50 + 3500))
        q = ln.quotes[0]
        self.assertGreaterEqual(q.margin, 5000, "권장가에서 최소 마진 이상")

    def test_batch_shares_fixed_costs(self):
        a = self.keycap(id="a", batch="A")
        b = self.keycap(id="b", batch="A", qty=10)
        lines = calculate(self.c, SC, self.fx, [a, b])
        self.assertAlmostEqual(sum(x.fixed_krw for x in lines), 43000, delta=2, msg="통관비는 묶음당 1번")
        single = calculate(self.c, SC, self.fx, [self.keycap(id="a")])[0]
        self.assertLess(lines[0].fixed_krw, single.fixed_krw, "같이 보내면 통관비 부담이 줄어듦")

    def test_min_freight(self):
        ln = calculate(self.c, SC, self.fx, [self.keycap(qty=1)])[0]
        self.assertEqual(ln.freight_krw, 30000, "해운 최소요금")

    def test_keyboard_blocked_unless_certified(self):
        kb = Item(id="kb", name="무선 키보드", category="keyboard", unit_price=160, qty=20, weight_kg=1.3)
        self.assertIn("사입 불가", calculate(self.c, SC, self.fx, [kb])[0].blocked)
        kb.cert_cost_krw = 2_000_000
        ln = calculate(self.c, SC, self.fx, [kb])[0]
        self.assertEqual(ln.blocked, "")
        self.assertTrue(any("인증비" in a for a in ln.alerts))

    def test_tax_types(self):
        gen = calculate(self.c, SC, self.fx, [self.keycap()])[0]
        simple = calculate(self.c, {**SC, "tax": {"type": "simple", "simple_vat_rate_of_sales": 0.015}},
                           self.fx, [self.keycap()])[0]
        self.assertGreater(simple.unit_cost, gen.unit_cost, "간이과세자는 수입 부가세가 원가에 포함")

    def test_market_price_check(self):
        ln = calculate(self.c, SC, self.fx, [self.keycap(market_prices={"coupang": 15000})])[0]
        self.assertFalse(ln.quotes[0].ok_at_market)
        self.assertTrue(any("시장가" in a for a in ln.alerts))


# ---------------------------------------------------------------- 네이버 검색어 트렌드 (API HUB)
class TestNaver(unittest.TestCase):
    def setUp(self):
        self.s = FakeServer()
        from core.naver import Naver
        self.nv = Naver({"client_id": "ID", "client_secret": "SEC", "api_base": self.s.url})

    def tearDown(self):
        self.s.close()

    def test_trend(self):
        from core.naver import sparkline, trend_change
        from datetime import date, timedelta
        d = date.today().replace(day=1)
        months = []
        for _ in range(12):                                      # 지난달부터 거꾸로 12개월
            d = (d - timedelta(days=1)).replace(day=1)
            months.insert(0, d.isoformat())
        vals = [10] * 9 + [20] * 3
        self.s.post_responses = [{"results": [
            {"title": "키캡", "data": [{"period": p, "ratio": v} for p, v in zip(months, vals)]},
            # 검색량이 적은 달은 응답에서 빠짐 → 0으로 채워 12개월을 유지
            {"title": "손목받침", "data": [{"period": p, "ratio": 5} for p in months if p not in months[3:6]]}]}]
        t = self.nv.trend(["키캡", "손목받침"])
        self.assertEqual([v for _, v in t["키캡"]], vals)
        self.assertEqual([p for p, _ in t["키캡"]], months)
        self.assertEqual([v for _, v in t["손목받침"]], [5] * 3 + [0] * 3 + [5] * 6)
        self.assertAlmostEqual(trend_change(vals), 1.0)          # 3개월 평균 10 → 20
        body = self.s.posts[0]
        self.assertEqual(body["timeUnit"], "month")
        self.assertEqual(body["keywordGroups"][0], {"groupName": "키캡", "keywords": ["키캡"]})
        self.assertTrue(body["endDate"] < __import__("datetime").date.today().replace(day=1).isoformat(),
                        "이번 달(덜 끝난 달)은 빼고")
        self.assertIsNone(trend_change([1, 2]))
        self.assertEqual(len(sparkline(vals)), 12)

    def test_hub_path_headers_and_batches(self):
        self.s.post_responses = [{"results": []}, {"results": []}]
        self.nv.trend([f"k{i}" for i in range(7)])
        self.assertEqual(len(self.s.posts), 2, "5개씩 나눠서 요청")
        self.assertEqual(len(self.s.posts[0]["keywordGroups"]), 5)
        self.assertEqual(self.s.post_paths[0], "/search-trend/v1/search")
        h = {k.lower(): v for k, v in self.s.post_headers[0].items()}    # HTTP 헤더 이름은 대소문자 무관
        self.assertEqual(h.get("x-ncp-apigw-api-key-id"), "ID")
        self.assertEqual(h.get("x-ncp-apigw-api-key"), "SEC")


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
