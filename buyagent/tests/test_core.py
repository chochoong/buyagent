"""회귀 테스트 (표준 라이브러리 unittest)

    python -m unittest            # buyagent 폴더에서 실행
    python -m unittest -v         # 항목별 결과 보기

실제 API·웹훅에는 접속하지 않습니다 (로컬 테스트 서버 사용).
"""
from __future__ import annotations

import contextlib
import copy
import csv
import io
import json
import os
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

    def test_stale_shipping_rates(self):
        from datetime import date
        from core.shipping_rates import stale_messages
        c = {"origins": {"JP": {"name": "일본"}}, "shipping_checked": {"max_days": 30, "JP": ""}}
        self.assertIn("확인 기록 없음", stale_messages(c)[0])
        c["shipping_checked"]["JP"] = date(2026, 11, 1)
        self.assertEqual(stale_messages(c, date(2026, 12, 1)), [])           # 30일째는 아직
        msg = stale_messages(c, date(2026, 12, 2))
        self.assertEqual(len(msg), 1)
        self.assertIn("2026-11-01", msg[0])
        self.assertEqual(msg, stale_messages(c, date(2026, 12, 20)))         # 날이 지나도 문장이 같음 → 알림 한 번

    def test_forwarding_in_result(self):
        from core.fx import FX
        from core.pricing import price_offer
        c = cfg()
        r = Result(spec=Spec(id="x", name="x", category="watch", keyword="x"))
        price_offer(c, FX(c, True), r, Offer("demo", "k", "n", 19700, "JPY", "JP", shipping=0, shipping_known=True))
        self.assertEqual((r.forwarding_krw, r.shipping_profile), (c["shipping_profiles"]["JP"]["medium"], "medium"))
        self.assertIn(f"배대지 {r.forwarding_krw:,}원 포함", " / ".join(M.fmt_result(r)))

    def test_small_shipping_hint(self):
        from core.fx import FX
        from core.pricing import price_offer
        c = cfg()

        def calc(name, category="watch", origin="JP"):
            r = Result(spec=Spec(id="x", name="x", category=category, keyword="x"))
            price_offer(c, FX(c, True), r, Offer("demo", "k", name, 851, "JPY", origin, text=name))
            return r
        r = calc("ちいかわ 鍵付きノート [M便 1/1]")                 # 시계(medium)로 계산 → 알림
        hint = [a for a in r.alerts if "소형 배송일 수 있음" in a]
        self.assertEqual(len(hint), 1)
        self.assertIn("'M便'", hint[0])
        small = c["shipping_profiles"]["JP"]["medium"] - c["shipping_profiles"]["JP"]["small"]
        self.assertIn(f"{r.cost_krw - small:,}원", hint[0])
        self.assertEqual(r.cost_krw, calc("ちいかわ ノート").cost_krw)    # 계산값은 안 바꿈
        self.assertFalse(any("소형" in a for a in calc("ちいかわ ノート").alerts))
        self.assertFalse(any("소형" in a for a in calc("ノート メール便", "stationery").alerts))   # 이미 small


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

    def test_shop_promo_words_not_limited(self):
        """라쿠텐 가게 이벤트 문구(포인트 추첨)·덤 표시는 정상 상품 (2026-11-18 세이코 88건 중 49건 오차단)"""
        for name in ["＼全品10%OFF+抽選100%還元★11/18限定／ セイコー メカニカル",
                     "【10％OFFクーポン&抽選で最大10000pt当たる!】【選べるノベルティー付き】セイコー アストロン",
                     "【ノベルティ付】【無金利ローン可】 セイコー アストロン", "【購入特典】 セイコー", "アルバム 特典付き"]:
            self.assertIsNone(blocked_category(CFG, name, "watch"), name)
        for name in ["一番くじ ちいかわ A賞", "ちいかわ 特典 ブロマイド のみ", "非売品 ノベルティ ポーチ",
                     "キャンペーン 当選品 アクリルスタンド"]:
            self.assertEqual(blocked_category(CFG, name, "watch"), "limited", name)

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
        offers, errors, skipped = run.fetch(Spec(id="d", name="d", category="acrylic", keyword="x",
                                                 discover=True), newest=True)
        self.assertEqual(offers, [])
        self.assertEqual(errors, [])
        self.assertTrue(any("최신순" in e for e in skipped), "야후에 요청하지 않고 이유를 알려줌")
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

    def test_only_manual_ready_alerts(self):
        """라쿠텐 키가 없고 manual 만 준비됨 → '소스 없음'이 묻히지 않고 실행 실패로 알림"""
        f = self.tmp / "manual.csv"
        f.write_text("id,price,currency,origin,shipping,in_stock,shop,url,checked\n", encoding="utf-8")
        c = self._cfg()                                    # 라쿠텐 키 없음
        c["sources"]["manual"] = {"enabled": True, "file": str(f)}
        args = type("A", (), {"demo": False, "bundle": 0})()
        specs = [Spec(id="a", name="a", category="watch", keyword="x", must_include=["x"])]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            problem = M.run_all(c, args, specs, specs)
        self.assertIn("검색할 쇼핑몰이 없습니다", problem)
        self.assertIn("모두 조회 실패", problem, "상품도 '재고 없음'이 아니라 조회 실패로 처리")

    def test_no_alert_skips_webhook_only(self):
        c = self._cfg(application_id="APP")
        item = {"itemCode": "s:1", "itemName": "セイコー SBTQ041", "itemPrice": 15000, "postageFlag": 0,
                "availability": 1, "shopName": "S", "itemUrl": "u"}
        self.s.responses = [(200, {"Items": [item]})] * 5
        M.load_config = lambda *a, **k: c
        (self.tmp / "catalog").mkdir()
        (self.tmp / "catalog" / "products.csv").write_text(
            "id,name,category,keyword,must_include,active\nSBTQ041,세이코,watch,セイコー SBTQ041,SBTQ041,Y\n",
            encoding="utf-8")
        sys.argv = ["main.py", "--no-alert"]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                M.main()
        self.assertEqual(cm.exception.code, 0)              # 일부 경고가 있어도 정상 실행이면 0
        self.assertEqual(self.s.posts, [], "웹훅을 보내면 안 됨")
        self.assertTrue((self.tmp / "history.db").exists(), "가격 기록은 평소대로")
        self.assertTrue(self._latest_ok())
        self.assertFalse((self.tmp / "health.json").exists())

    def _latest_ok(self):
        d = json.loads((self.tmp / "reports" / "latest.json").read_text(encoding="utf-8"))
        return d["ok"] and d["items"][0]["status"] == "ok"


# ---------------------------------------------------------------- 일부 쇼핑몰만 실패 / 가격 0
class FakeSrc:
    explicit_only = False
    supports_newest = True

    def __init__(self, offers=None, error=""):
        self.offers, self.error = offers or [], error

    def search(self, q):
        if self.error:
            raise RuntimeError(self.error)
        return self.offers


class TestPartialFailure(TmpDirTest):
    def setUp(self):
        super().setUp()
        c = cfg()
        c["fx"]["auto"] = False
        self.run = M.Runner(c, False, 0)
        self.run.store = Store(self.tmp / "t.db")
        self.spec = Spec(id="p", name="p", category="kitchen", keyword="x")

    def test_partial_failure_no_fake_soldout(self):
        instock = Offer("b", "b1", "x", 4000, "JPY", "JP")
        soldout = Offer("a", "a1", "x", 3000, "JPY", "JP", in_stock=False)
        self.run.sources = {"a": FakeSrc([soldout]), "b": FakeSrc([instock])}
        self.assertIsNotNone(self.run.run_product(self.spec).offer)        # 기록: 재고 있음
        self.run.sources["b"] = FakeSrc(error="HTTP 503")                   # 재고 있던 쪽만 일시 실패
        r = self.run.run_product(self.spec)
        self.assertTrue(r.failed)
        self.assertFalse(any("재고 없음" in e for e in r.events), "가짜 품절 알림 없음")
        self.assertTrue(any("503" in a for a in r.alerts), "실패한 쇼핑몰을 알려줌")
        self.run.sources["b"] = FakeSrc([instock])                          # 복구
        r = self.run.run_product(self.spec)
        self.assertEqual(r.events, [], "가짜 재입고 알림도 없음")

    def test_zero_price_ignored(self):
        free = Offer("a", "z", "x", 0, "CNY", "CN")
        real = Offer("a", "r", "x", 299, "CNY", "CN")
        self.run.sources = {"a": FakeSrc([free, real])}
        r = self.run.run_product(self.spec)
        self.assertFalse(r.failed)
        self.assertEqual(r.offer.key, "r", "가격 0은 최저가로 뽑히지 않음")
        self.assertEqual(r.excluded.get("가격 없음"), 1)


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

    def test_env_conf_reads_dotenv(self):
        from core.naver import env_conf
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / ".env").write_text("NAVER_CLIENT_ID=abc\nNAVER_CLIENT_SECRET=xyz\n", encoding="utf-8")
            saved = {k: os.environ.pop(k, None) for k in ("NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET")}
            try:
                c = env_conf(tmp, {"api_base": "http://x"})
                self.assertEqual((c["client_id"], c["client_secret"], c["api_base"]), ("abc", "xyz", "http://x"))
            finally:
                for k, v in saved.items():
                    os.environ.pop(k, None)
                    if v is not None:
                        os.environ[k] = v
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

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



class TestRakutenWeb(unittest.TestCase):
    """API 키 없이 검색 페이지를 읽는 소스: HTML 파싱만 검사 (크롬은 띄우지 않음)"""
    CARD = ('<div class="dui-card searchresultitem x--1" data-id="{iid}" data-shop-id="77" '
            'data-track-price="{p}"><a href="https://item.rakuten.co.jp/shop/{iid}/?rafcid=1">img</a>'
            '<h2><a title="t" data-link="item" href="https://item.rakuten.co.jp/shop/{iid}/">'
            'スタンレー &amp; タンブラー {iid}</a></h2><div class="price--3"><div class="price--x">{p}'
            '<span>円〜</span></div>{free}</div><div class="points" data-price="{p}" data-max-price="{mx}"></div>'
            '<div class="content merchant _ellipsis"><span class="dui-icon"></span>'
            '<a href="https://www.rakuten.co.jp/shop/">テスト店</a></div></div>')

    def card(self, iid, p, mx=None, free=True):
        return self.CARD.format(iid=iid, p=p, mx=mx or p,
                                free='<span class="free-shipping-label--1">送料無料</span>' if free else "")

    def test_parse(self):
        from sources.rakuten_web import parse_search_html
        page = "<html>" + self.card(1, 3300, 5170) + self.card(2, 900, free=False) + self.card(1, 3300) + "</html>"
        items = parse_search_html(page)
        self.assertEqual(len(items), 2)                      # 같은 상품(광고 중복)은 한 번만
        a, b = items
        self.assertEqual((a["price"], a["max_price"], a["free_shipping"]), (3300, 5170, True))
        self.assertEqual(a["name"], "スタンレー & タンブラー 1")
        self.assertEqual(a["shop"], "テスト店")
        self.assertEqual(a["url"], "https://item.rakuten.co.jp/shop/1/")
        self.assertFalse(b["free_shipping"])

    def test_blocked_page_raises(self):
        src = REGISTRY["rakuten_web"]({"enabled": True, "min_interval": 0, "retries": 0}, CFG)
        src.fetch_html = lambda url: "Reference #18.abc"     # 봇 차단 응답 → 0건(품절)이 아니라 오류
        with self.assertRaises(RuntimeError):
            src.search(Query("x"))

    def test_new_card_class_dui_item(self):
        """2026-11 라쿠텐이 카드 class 를 dui-card → dui-item 으로 바꿈 → 둘 다 읽혀야 함"""
        from sources.rakuten_web import parse_search_html
        page = self.card(1, 3300) + self.card(2, 900).replace("dui-card searchresultitem", "dui-item searchresultitem")
        self.assertEqual([i["item_id"] for i in parse_search_html(page)], ["1", "2"])

    def test_real_no_result_page_is_empty_not_error(self):
        src = REGISTRY["rakuten_web"]({"enabled": True, "min_interval": 0, "retries": 0}, CFG)
        src.fetch_html = lambda url: "<html>" + "x" * 5000 + "ご指定の検索条件に該当する商品はみつかりませんでした</html>"
        self.assertEqual(src.search(Query("x")), [])

    def test_unreadable_cards_raise_structure_change(self):
        """카드 표시는 있는데 하나도 못 읽으면 조용히 0건(가짜 품절)이 아니라 '구조 변경' 오류"""
        src = REGISTRY["rakuten_web"]({"enabled": True, "min_interval": 0, "retries": 0}, CFG)
        src.fetch_html = lambda url: ("<html>" + "x" * 5000 + '<li class="new-card searchresultitem">상품</li>' * 3
                                      + "</html>")
        with self.assertRaises(RuntimeError) as cm:
            src.search(Query("x"))
        self.assertIn("구조 변경", str(cm.exception))

    def test_offers(self):
        src = REGISTRY["rakuten_web"]({"enabled": True, "min_interval": 0, "default_shipping": 800}, CFG)
        src.fetch_html = lambda url: self.card(1, 3300, 5170) + self.card(2, 900, free=False)
        o1, o2 = src.search(Query("x"))
        self.assertEqual((o1.key, o1.shipping, o1.shipping_known), ("rkw:77:1", 0, True))
        self.assertTrue(o1.notes)                            # 옵션별 가격 차이 안내
        self.assertEqual((o2.shipping, o2.shipping_known), (800, False))


class TestManualShipping(TmpDirTest):
    """수동 가격의 shipping 빈 칸은 '무료배송 확인'이 아니라 '미확인' (원가 과소평가 방지)"""

    def setUp(self):
        super().setUp()
        self.saved = M.BASE
        M.BASE = self.tmp                                  # Runner 가 만드는 DB 를 임시 폴더로

    def tearDown(self):
        M.BASE = self.saved
        super().tearDown()

    def _offers(self):
        f = self.tmp / "mp.csv"
        f.write_text("id,price,currency,origin,shipping,in_stock,shop,url,checked,memo\n"
                     "a,10,USD,CN,,Y,blank,,2026-11-17,\n"
                     "a,12,USD,CN,0,Y,zero,,2026-11-17,\n"
                     "a,14,USD,CN,3.5,Y,num,,2026-11-17,\n", encoding="utf-8")
        src = REGISTRY["manual"]({"enabled": True, "file": str(f)}, CFG)
        return src.search(Query("x", spec_id="a"))

    def test_three_cases(self):
        blank, zero, num = self._offers()
        self.assertEqual((blank.shipping, blank.shipping_known), (0, False))
        self.assertEqual((zero.shipping, zero.shipping_known), (0, True))
        self.assertEqual((num.shipping, num.shipping_known), (3.5, True))

    def test_alert(self):
        blank, zero, num = self._offers()
        run = M.Runner(cfg(), True, 0)
        spec = Spec(id="a", name="a", category="watch", keyword="x")
        msg = "현지 배송비 미확인 → 확인 후 shipping 입력"
        self.assertTrue(any(msg in a for a in run.priced(spec, blank).alerts))
        for o in (zero, num):
            self.assertFalse(any("배송비 미확인" in a for a in run.priced(spec, o).alerts))


class TestHubProductId(TmpDirTest):
    HEAD = ("id,name,category,keyword,jan,must_include,exclude,trusted_shops,sources,"
            "shipping_profile,bundle_size,my_price_coupang,my_price_naver,active,memo")

    def _write(self, text):
        (self.tmp / "catalog").mkdir(exist_ok=True)
        (self.tmp / "catalog" / "products.csv").write_text(text, encoding="utf-8")
        return load_specs(self.tmp, CFG["markets"])

    def test_old_csv_without_column(self):
        specs = self._write(self.HEAD + "\na,A,watch,x,,,,,,,,0,0,Y,\n")
        self.assertIsNone(specs[0].hub_product_id)

    def test_column(self):
        specs = self._write(self.HEAD + ",hub_product_id\n"
                            "a,A,watch,x,,,,,,,,0,0,Y,,23\n"
                            "b,B,watch,y,,,,,,,,0,0,Y,,\n"
                            "c,C,watch,z,,,,,,,,0,0,Y,,2x\n")
        self.assertEqual([s.hub_product_id for s in specs], [23, None, None])
        issues = [m for _, m in validate(CFG, specs, REGISTRY, self.tmp)]
        self.assertTrue(any("hub_product_id '2x'" in m for m in issues))
        self.assertEqual(sum("hub_product_id" in m for m in issues), 1)


class TestLatestJson(TmpDirTest):
    """--demo 실행 → reports/latest.json 형식 (trade_hub 대시보드가 읽는 키)"""

    def setUp(self):
        super().setUp()
        self.saved = (M.BASE, M.HEALTH_FILE, M.load_config, sys.argv)
        shutil.copytree(BASE / "catalog", self.tmp / "catalog")
        shutil.copytree(BASE / "config", self.tmp / "config",
                        ignore=shutil.ignore_patterns("settings.local.toml*"))   # 실제 키는 복사 안 함
        M.BASE, M.HEALTH_FILE = self.tmp, self.tmp / "health.json"
        # 실제 products.csv 에서 사용자가 꺼 둔 상품도 켜서 시험 (차단·수동 가격 예시가 빠지지 않게)
        pc = self.tmp / "catalog" / "products.csv"
        with pc.open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        with pc.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows({**r, "active": "Y"} for r in rows)

    def tearDown(self):
        M.BASE, M.HEALTH_FILE, M.load_config, sys.argv = self.saved
        super().tearDown()

    def _main(self, *argv):
        sys.argv = ["main.py", *argv]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                M.main()
        return cm.exception.code

    def _latest(self):
        return json.loads((self.tmp / "reports" / "latest.json").read_text(encoding="utf-8"))

    def test_demo_format(self):
        self.assertEqual(self._main("--demo"), 0)
        d = self._latest()
        self.assertTrue({"error", "demo", "markets", "items"} <= set(d))
        self.assertIsNone(d["error"])
        self.assertTrue(d["ok"] and d["demo"])
        self.assertEqual(d["markets"], list(CFG["markets"]))
        keys = {"kind", "id", "hub_product_id", "name", "category", "status", "blocked_reason",
                "best_offer", "cost_krw", "forwarding_krw", "shipping_profile", "quotes", "warnings", "events", "notice"}
        offer_keys = {"source", "shop", "url", "price", "shipping", "shipping_known", "currency", "in_stock"}
        self.assertTrue(d["items"])
        for it in d["items"]:
            self.assertEqual(set(it), keys)
            self.assertIn(it["kind"], ("감시", "신상"))
            self.assertIn(it["status"], ("ok", "blocked", "no_offer", "error"))
            self.assertIsInstance(it["warnings"], list)
            self.assertIsInstance(it["events"], list)
            if it["best_offer"]:
                self.assertEqual(set(it["best_offer"]), offer_keys)
                if not it["best_offer"]["shipping_known"]:
                    self.assertIsNone(it["best_offer"]["shipping"])
            for q in it["quotes"]:
                self.assertEqual(set(q), {"market", "recommended_price", "margin", "my_price", "my_margin"})
                self.assertIn(q["market"], d["markets"])
            if it["status"] == "blocked":
                self.assertTrue(it["blocked_reason"])
        self.assertIn("blocked", {i["status"] for i in d["items"]})
        self.assertEqual(list((self.tmp / "reports").glob(".latest_*")), [], "임시 파일이 남으면 안 됨")

    def test_paused_skips_watch_run(self):
        from core.pause import set_paused
        set_paused(self.tmp, True)
        self.assertEqual(self._main("--no-alert"), 0)                     # 멈춤 → 조회 없이 정상 종료
        self.assertFalse((self.tmp / "reports" / "latest.json").exists())
        self.assertEqual(self._main("--demo"), 0)                         # 예시 실행은 막지 않음
        self.assertTrue((self.tmp / "reports" / "latest.json").exists())
        set_paused(self.tmp, False)

    def test_failure_writes_error_and_exit_code(self):
        c = load_config(self.tmp)
        c["sources"] = {}
        M.load_config = lambda *a, **k: c
        self.assertEqual(self._main("--no-alert"), 1)       # 실행 실패 → 0 이 아닌 종료 코드
        d = self._latest()
        self.assertFalse(d["ok"])
        self.assertIn("사용 가능한 소스가 없습니다", d["error"])
        self.assertFalse((self.tmp / "health.json").exists(), "--no-alert 는 health.json 을 바꾸지 않음")

    def test_run_at(self):
        self._main("--demo")
        self.assertRegex(self._latest()["run_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")   # 대시보드가 읽는 키

    def test_search(self):
        self.assertEqual(self._main("--demo", "--search", "セイコー SBTQ041", "--category", "watch"), 0)
        d = json.loads((self.tmp / "reports" / "search.json").read_text(encoding="utf-8"))
        self.assertTrue(d["ok"])
        self.assertEqual(d["query"]["must_include"], ["SBTQ041"])            # 검색어 속 모델명
        self.assertIn("ベルト", d["query"]["exclude"])                        # 카테고리 search_exclude
        self.assertTrue(d["items"])
        costs = [i["cost_krw"] for i in d["items"]]
        self.assertEqual(costs, sorted(costs))                               # 원가 낮은 순
        self.assertTrue(all(i["kind"] == "검색" and "SBTQ041" in i["name"] for i in d["items"]))
        self.assertFalse((self.tmp / "reports" / "latest.json").exists(), "감시 결과는 건드리지 않음")
        self.assertFalse((self.tmp / "health.json").exists())

    def _search_ko(self, call, query="세이코  SBTQ041"):
        saved = M.TRANSLATE_CALL
        M.TRANSLATE_CALL = call
        out = io.StringIO()
        try:
            sys.argv = ["main.py", "--demo", "--search", query, "--category", "watch"]
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as cm:
                    M.main()
        finally:
            M.TRANSLATE_CALL = saved
        return cm.exception.code, out.getvalue()

    def test_search_korean_is_translated(self):
        calls = []
        code, out = self._search_ko(lambda q, base: calls.append(q) or "「セイコー SBTQ041」")
        self.assertEqual(code, 0)
        self.assertEqual(calls, ["세이코 SBTQ041"])                          # 공백 정리 후 1번 호출
        d = json.loads((self.tmp / "reports" / "search.json").read_text(encoding="utf-8"))
        self.assertEqual(d["query"]["keyword"], "セイコー SBTQ041")           # 따옴표·「」 제거
        self.assertEqual(d["query"]["keyword_original"], "세이코 SBTQ041")
        self.assertEqual(d["query"]["must_include"], ["SBTQ041"])
        self.assertTrue(d["items"])
        self.assertIn("번역: 세이코 SBTQ041 → セイコー SBTQ041", out)
        # 같은 검색어 → 캐시 (번역 함수를 부르지 않음)
        def boom(q, base):
            raise AssertionError("캐시를 써야 함")
        self.assertEqual(self._search_ko(boom, "세이코 SBTQ041")[0], 0)
        self.assertTrue((self.tmp / "data" / "translate_cache.json").exists())

    def test_search_japanese_has_no_original(self):
        code, _ = self._search_ko(lambda q, base: self.fail("한글이 없으면 번역 안 함"), "セイコー SBTQ041")
        self.assertEqual(code, 0)
        d = json.loads((self.tmp / "reports" / "search.json").read_text(encoding="utf-8"))
        self.assertIsNone(d["query"]["keyword_original"])

    def test_search_translate_failure(self):
        bad = {"한글 남음": lambda q, b: "セイコー 시계", "빈 답": lambda q, b: "  ",
               "여러 줄": lambda q, b: "セイコー\nSBTQ041", "너무 김": lambda q, b: "ア" * 101,
               "API 오류": lambda q, b: (_ for _ in ()).throw(ConnectionError("x"))}
        for why, call in bad.items():
            code, out = self._search_ko(call)
            self.assertEqual(code, 2, why)
            self.assertIn("번역 실패: 일본어로 직접 입력해 주세요", out, why)
            self.assertFalse((self.tmp / "reports" / "search.json").exists(), f"{why}: search.json 안 씀")
            self.assertFalse((self.tmp / "data" / "translate_cache.json").exists(), f"{why}: 실패는 캐시 안 함")

    def test_translate_units(self):
        from core import translate as T
        none = lambda q, b: self.fail("부르면 안 됨")                       # noqa: E731
        self.assertEqual(T.to_japanese("セイコー  SBTQ041", self.tmp, call=none), "セイコー SBTQ041")
        self.assertEqual(T.to_japanese("쿠션 S", self.tmp, {"쿠션": ["クッション"]}, call=none), "クッション S")
        import os
        saved = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            with self.assertRaises(T.TranslateError):                     # 키 없음 (임시 폴더엔 .env 없음)
                T.to_japanese("새 단어", self.tmp)
        finally:
            if saved is not None:
                os.environ["ANTHROPIC_API_KEY"] = saved

    def test_search_needs_category(self):
        self.assertEqual(self._main("--search", "x"), 2)
        self.assertEqual(M.model_words("セイコー SBTQ041 腕時計 7T92"), ["SBTQ041", "7T92"])

    def _no_ali_keys(self):
        cls = REGISTRY["aliexpress"]
        saved = cls.keys
        cls.keys = lambda self: {k: "" for k in cls.env_keys}
        self.addCleanup(lambda: setattr(cls, "keys", saved))

    def test_search_market_aliexpress_without_keys(self):
        self._no_ali_keys()
        self.assertEqual(self._main("--search", "Seiko SBTQ041", "--category", "watch", "--market", "aliexpress"), 1)
        d = json.loads((self.tmp / "reports" / "search.json").read_text(encoding="utf-8"))
        self.assertFalse(d["ok"])
        self.assertIn("ALIEXPRESS_APP_KEY", d["error"])            # 키 없음 → '상품 없음'이 아니라 실패
        self.assertEqual(d["query"]["market"], "aliexpress")
        self.assertEqual(d["query"]["exclude"], [])                 # 일본어 search_exclude 는 알리에 안 씀
        self.assertEqual(d["items"], [])

    def test_search_market_default_is_rakuten(self):
        self.assertEqual(self._main("--demo", "--search", "セイコー SBTQ041", "--category", "watch"), 0)
        d = json.loads((self.tmp / "reports" / "search.json").read_text(encoding="utf-8"))
        self.assertEqual(d["query"]["market"], "rakuten")
        self.assertIn("ベルト", d["query"]["exclude"])

    def test_search_market_ebay_without_keys(self):
        cls = REGISTRY["ebay"]
        saved = cls.keys
        cls.keys = lambda self: {k: "" for k in cls.env_keys}
        self.addCleanup(lambda: setattr(cls, "keys", saved))
        self.assertEqual(self._main("--search", "Seiko SBTQ041", "--category", "watch", "--market", "ebay"), 1)
        d = json.loads((self.tmp / "reports" / "search.json").read_text(encoding="utf-8"))
        self.assertIn("EBAY_CLIENT_ID", d["error"])
        self.assertEqual(d["query"]["market"], "ebay")

    def test_search_market_invalid(self):
        self.assertEqual(self._main("--search", "x", "--category", "watch", "--market", "taobao"), 2)

    def test_search_korean_to_english_for_aliexpress(self):
        self._no_ali_keys()
        calls = []
        saved = M.TRANSLATE_CALL
        M.TRANSLATE_CALL = lambda q, base: calls.append(q) or "Xiaomi smart glasses"
        try:
            self._main("--search", "샤오미 스마트 안경", "--category", "watch", "--market", "aliexpress")
        finally:
            M.TRANSLATE_CALL = saved
        d = json.loads((self.tmp / "reports" / "search.json").read_text(encoding="utf-8"))
        self.assertEqual(calls, ["샤오미 스마트 안경"])
        self.assertEqual((d["query"]["keyword"], d["query"]["keyword_original"]),
                         ("Xiaomi smart glasses", "샤오미 스마트 안경"))
        cache = json.loads((self.tmp / "data" / "translate_cache.json").read_text(encoding="utf-8"))
        self.assertEqual(cache, {"en:샤오미 스마트 안경": "Xiaomi smart glasses"})   # 일본어 캐시와 안 섞임

    def test_crash_writes_error(self):
        def boom(*a, **k):
            raise ValueError("문법 오류")
        M.load_config = boom
        self.assertEqual(self._main("--no-alert"), 1)
        d = self._latest()
        self.assertIn("문법 오류", d["error"])
        self.assertEqual(d["items"], [])


ALI_OK = {"aliexpress_affiliate_product_query_response": {"resp_result": {"resp_code": 200, "resp_msg": "success",
    "result": {"current_record_count": 3, "products": {"product": [
        {"product_id": 1005001, "product_title": "Seiko SBTQ041 Watch", "target_sale_price": "95.50",
         "target_sale_price_currency": "USD", "target_original_price": "120.00", "shop_name": "Watch Store",
         "shop_id": 9, "product_detail_url": "https://www.aliexpress.com/item/1005001.html?aff=x"},
        {"product_id": 1005002, "product_title": "No price", "target_sale_price": "",
         "target_sale_price_currency": "USD"},
        {"product_id": 1005003, "product_title": "Other currency", "target_sale_price": "700",
         "target_sale_price_currency": "CNY"}]}}}}}


class TestAliExpress(unittest.TestCase):
    """키 없이 가짜 응답으로 (실제 API 호출 없음)"""

    def setUp(self):
        self.cfg = load_config(BASE)
        self.src = REGISTRY["aliexpress"](self.cfg["sources"]["aliexpress"], self.cfg)
        self.src.keys = lambda: {"ALIEXPRESS_APP_KEY": "k", "ALIEXPRESS_APP_SECRET": "s",
                                 "ALIEXPRESS_TRACKING_ID": "t"}
        self.sent = []
        self.src.request = lambda p: self.sent.append(p) or copy.deepcopy(ALI_OK)

    def test_search_to_offers(self):
        from sources.aliexpress import sign
        offers = self.src.search(Query("Seiko SBTQ041"))
        self.assertEqual([o.key for o in offers], ["ali:1005001"])      # 가격 없음·다른 통화는 뺌
        o = offers[0]
        self.assertEqual((o.price, o.currency, o.origin, o.shop), (95.5, "USD", "CN_DIRECT", "Watch Store"))
        self.assertEqual(o.url, "https://www.aliexpress.com/item/1005001.html")   # 제휴 추적 파라미터 제거
        self.assertFalse(o.shipping_known)
        p = self.sent[0]
        self.assertEqual((p["method"], p["keywords"], p["ship_to_country"], p["tracking_id"]),
                         ("aliexpress.affiliate.product.query", "Seiko SBTQ041", "KR", "t"))
        self.assertEqual(p["sign"], sign({k: v for k, v in p.items() if k != "sign"}, "s"))

    def test_sign_known_value(self):
        import hashlib
        import hmac
        from sources.aliexpress import sign
        want = hmac.new(b"sec", b"a1b2methodx", hashlib.sha256).hexdigest().upper()
        self.assertEqual(sign({"method": "x", "b": "2", "a": "1", "sign": "old"}, "sec"), want)  # 이름순, sign 제외

    def test_errors_are_not_silent_zero(self):
        from sources.aliexpress import parse_products
        for bad in ({"error_response": {"code": "IncompleteSignature", "msg": "bad sign"}},
                    {"code": "IllegalAccessToken", "message": "x"},
                    {"aliexpress_affiliate_product_query_response": {"resp_result": {"resp_code": 402,
                                                                                     "resp_msg": "bad"}}},
                    []):
            with self.assertRaises(RuntimeError, msg=str(bad)):
                parse_products(bad)
        empty = {"aliexpress_affiliate_product_query_response": {"resp_result": {"resp_code": 405}}}
        self.assertEqual(parse_products(empty), [])

    def test_explicit_only_and_pricing_without_forwarder(self):
        from core.fx import FX
        from core.pricing import price_offer
        self.assertTrue(REGISTRY["aliexpress"].explicit_only)       # 일반 감시(일본어 검색어)에는 안 쓰임
        offer = self.src.search(Query("Seiko SBTQ041"))[0]
        fx = FX(self.cfg, True)
        r = Result(spec=Spec(id="x", name="x", category="watch", keyword="x"))
        price_offer(self.cfg, fx, r, offer)
        c = self.cfg["costs"]
        goods = fx.to_krw(offer.total, "USD") * (1 + c["card_fx_fee_rate"])
        # 배대지 요금 0 (직배송) → 원가 = 물품(+카드 수수료) + 포장 + 관부가세(면세 이하면 0)
        self.assertEqual(r.cost_krw, int(round(goods + c["domestic_shipping_krw"] + c["packaging_krw"]
                                               + r.est_duty_krw)))
        self.assertTrue(any("현지 배송비 미확인" in a for a in r.alerts))

    def test_validate_warns_missing_env_keys(self):
        import os
        cls = REGISTRY["aliexpress"]
        saved = {k: os.environ.pop(k, None) for k in cls.env_keys}
        tmp = Path(tempfile.mkdtemp())
        try:
            issues = validate(self.cfg, [], REGISTRY, tmp)        # 임시 폴더 = .env 없음
            self.assertTrue(any("ALIEXPRESS_APP_KEY" in m for _, m in issues))
            self.assertFalse(any(lv == ERROR and "CN_DIRECT" in m for lv, m in issues))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

    def test_translate_english(self):
        from core import translate as T
        tmp = Path(tempfile.mkdtemp())
        try:
            self.assertEqual(T.to_lang("쿠션 S", tmp, "en", {"쿠션": ["クッション"]}, call=lambda q, b: "Cushion S"),
                             "Cushion S")                                   # 일본어 단어장은 en 에 안 씀
            with self.assertRaises(T.TranslateError) as cm:
                T.to_lang("새 단어", tmp, "en", call=lambda q, b: "セイコー watch")   # 영어 아닌 글자 → 실패
            self.assertIn("영어로 직접 입력", str(cm.exception))
            with self.assertRaises(ValueError):
                T.to_lang("새", tmp, "zh")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

EBAY_OK = {"total": 3, "itemSummaries": [
    {"itemId": "v1|111|0", "title": "Seiko SBTQ041 Watch", "price": {"value": "120.00", "currency": "USD"},
     "condition": "New", "seller": {"username": "watchshop", "feedbackPercentage": "99.5"},
     "shippingOptions": [{"shippingCostType": "FIXED", "shippingCost": {"value": "8.50", "currency": "USD"}}],
     "itemWebUrl": "https://www.ebay.com/itm/111?hash=x"},
    {"itemId": "v1|222|0", "title": "Seiko free ship", "price": {"value": "99.99", "currency": "USD"},
     "seller": {"username": "lowrep", "feedbackPercentage": "91.0"},
     "shippingOptions": [{"shippingCostType": "FIXED", "shippingCost": {"value": "0.00", "currency": "USD"}}],
     "itemWebUrl": "https://www.ebay.com/itm/222"},
    {"itemId": "v1|333|0", "title": "No shipping info", "price": {"value": "50", "currency": "USD"},
     "itemWebUrl": "https://www.ebay.com/itm/333"}]}


class TestEbay(unittest.TestCase):
    """키 없이 가짜 응답으로 (실제 API 호출 없음)"""

    def setUp(self):
        from sources.ebay import Ebay
        Ebay._token = ("", 0.0)
        self.addCleanup(lambda: setattr(Ebay, "_token", ("", 0.0)))
        self.cfg = load_config(BASE)
        self.make()

    def make(self, **conf):
        self.src = REGISTRY["ebay"]({**self.cfg["sources"]["ebay"], **conf}, self.cfg)
        self.src.keys = lambda: {"EBAY_CLIENT_ID": "id", "EBAY_CLIENT_SECRET": "sec"}
        self.tokens, self.sent = [], []
        self.src.token_request = lambda i, s: self.tokens.append((i, s)) or {"access_token": "TOK", "expires_in": 7200}
        self.src.request = lambda p, h: self.sent.append((p, h)) or copy.deepcopy(EBAY_OK)

    def test_search_to_offers(self):
        offers = self.src.search(Query("Seiko SBTQ041"))
        self.assertEqual([o.key for o in offers], ["ebay:v1|111|0", "ebay:v1|222|0", "ebay:v1|333|0"])
        a, b, c = offers
        self.assertEqual((a.price, a.currency, a.origin, a.shop, a.url),
                         (120.0, "USD", "US", "watchshop", "https://www.ebay.com/itm/111"))
        self.assertEqual((a.shipping, a.shipping_known), (8.5, False))    # 배대지 우편번호 없음 → 미확인
        self.assertEqual((b.shipping, b.shipping_known), (0.0, True))     # 무료배송은 확인됨
        self.assertTrue(any("판매자 평가 91.0%" in n for n in b.notes))
        self.assertEqual((c.shipping, c.shipping_known), (10.0, False))   # 정보 없음 → default_shipping
        params, headers = self.sent[0]
        self.assertEqual(params["q"], "Seiko SBTQ041")
        for f in ("conditions:{NEW}", "buyingOptions:{FIXED_PRICE}", "itemLocationCountry:US"):
            self.assertIn(f, params["filter"])                           # 중고·경매 제외
        self.assertEqual(headers["Authorization"], "Bearer TOK")
        self.assertNotIn("X-EBAY-C-ENDUSERCTX", headers)
        self.src.search(Query("x"))
        self.assertEqual(self.tokens, [("id", "sec")])                    # 토큰은 한 번만 받아 재사용

    def test_forwarder_zip_makes_shipping_known(self):
        self.make(forwarder_zip="19720")
        a = self.src.search(Query("Seiko"))[0]
        self.assertTrue(a.shipping_known)
        self.assertIn("zip%3D19720", self.sent[0][1]["X-EBAY-C-ENDUSERCTX"])

    def test_errors_are_not_silent_zero(self):
        from sources.ebay import parse_items
        with self.assertRaises(RuntimeError):
            parse_items({"errors": [{"errorId": 1001, "message": "Invalid access token"}]})
        with self.assertRaises(RuntimeError):
            parse_items([])
        self.assertEqual(parse_items({"total": 0}), [])
        self.src.token_request = lambda i, s: {"error": "invalid_client", "error_description": "client authentication failed"}
        with self.assertRaises(RuntimeError) as cm:
            self.src.search(Query("x"))
        self.assertIn("client authentication failed", str(cm.exception))
        import io as _io
        import urllib.error

        def raise_401(i, s):
            raise urllib.error.HTTPError("u", 401, "Unauthorized", {},
                                         _io.BytesIO(b'{"error":"invalid_client","error_description":"client authentication failed"}'))
        self.src.token_request = raise_401
        with self.assertRaises(RuntimeError) as cm:
            self.src.search(Query("x"))
        self.assertIn("HTTP 401 client authentication failed", str(cm.exception))
        self.assertIn("Production 키셋", str(cm.exception))

    def test_explicit_only_and_us_forwarder_pricing(self):
        from core.fx import FX
        from core.pricing import forwarding_fee, price_offer
        self.assertTrue(REGISTRY["ebay"].explicit_only)
        offer = self.src.search(Query("Seiko"))[0]
        r = Result(spec=Spec(id="x", name="x", category="watch", keyword="x"))
        price_offer(self.cfg, FX(self.cfg, True), r, offer)
        self.assertGreater(r.cost_krw, forwarding_fee(self.cfg, "US", "medium"))   # 미국 배대지 요금 포함

    def test_market_ebay_listed(self):
        import web as W
        self.assertIn("ebay", M.SEARCH_MARKETS)
        self.assertEqual(M.SEARCH_MARKETS["ebay"]["lang"], "en")
        self.assertEqual(set(M.SEARCH_MARKETS), set(W.SEARCH_MARKETS))   # 화면과 CLI 값이 같음
        self.assertEqual(W.MARKET_SOURCES["ebay"], "ebay")

if __name__ == "__main__":
    unittest.main()
