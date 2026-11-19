"""web.py (buyagent 자체 화면) 테스트 — 실제 catalog·reports 는 건드리지 않고 임시 폴더에서"""
from __future__ import annotations

import csv
import io
import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import web as W  # noqa: E402

SEARCH = {
    "ok": True, "error": None, "run_at": "2026-11-18T10:44:00",
    "query": {"keyword": "セイコー SBTQ043", "category": "watch", "must_include": ["SBTQ043"],
              "exclude": ["ベルト", "バンド"]},
    "markets": ["coupang"], "found": 90, "excluded": {"상품명 불일치": 25},
    "items": [{"kind": "검색", "id": "search", "hub_product_id": None, "name": "セイコー SBTQ041 <正規品>",
               "category": "watch", "status": "ok", "blocked_reason": None,
               "best_offer": {"source": "rakuten_web", "shop": "スリーエス", "url": "javascript:alert(1)",
                              "price": 14630, "shipping": None, "shipping_known": False, "currency": "JPY",
                              "in_stock": True},
               "cost_krw": 140565, "quotes": [{"market": "coupang", "recommended_price": 182900, "margin": 20405,
                                               "my_price": None, "my_margin": None}],
               "warnings": ["한정·예약 상품"], "events": [], "notice": None}],
}


class WebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copytree(BASE / "catalog", self.tmp / "catalog")
        (self.tmp / "config").mkdir()
        for f in ("categories.toml", "settings.toml"):
            shutil.copy(BASE / "config" / f, self.tmp / "config" / f)
        (self.tmp / "reports").mkdir()
        (self.tmp / "reports" / "search.json").write_text(json.dumps(SEARCH, ensure_ascii=False), encoding="utf-8")
        W.JOB.clear()

    def tearDown(self):
        W.JOB.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def products(self):
        with open(self.tmp / "catalog" / "products.csv", newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))

    # ---- 화면
    def test_render_escapes_and_blocks_bad_links(self):
        page = W.render(self.tmp)
        self.assertIn("セイコー SBTQ041 &lt;正規品&gt;", page)
        self.assertNotIn('href="javascript', page)
        self.assertIn("140,565원", page)
        self.assertIn("쿠팡 권장가", page)                       # 마켓 키 → 이름
        self.assertIn("배송비 미확인", page)
        self.assertIn('data-act="add"', page)
        self.assertIn("공급처 발굴에서", page)                    # 2번에서 넘어온 AI안경도 같은 목록에

    def test_render_shows_korean_original(self):
        d = json.loads(json.dumps(SEARCH))
        d["query"]["keyword_original"] = "세이코 SBTQ043"
        (self.tmp / "reports" / "search.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
        page = W.render(self.tmp)
        self.assertIn("← 세이코 SBTQ043 (자동 번역)", page)
        self.assertIn('value="세이코 SBTQ043"', page)                 # 검색창엔 원래 한글
        self.assertIn('data-name="세이코 SBTQ043"', page)             # 감시 추가 이름 기본값도 한글

    def test_render_without_results(self):
        (self.tmp / "reports" / "search.json").unlink()
        page = W.render(self.tmp)
        self.assertIn("아직 검색한 결과가 없어요", page)
        self.assertIn("아직 감시를 실행한 적이 없어요", page)

    # ---- 감시 목록 쓰기
    def test_add_watch(self):
        before = self.products()
        code, out = W.add_watch(self.tmp, SEARCH, "세이코 SBTQ043 (검색)")
        self.assertEqual(code, 200)
        rows = self.products()
        self.assertEqual(len(rows), len(before) + 1)
        self.assertEqual(rows[:-1], before)                      # 기존 줄은 그대로
        new = rows[-1]
        self.assertEqual(new["id"], "sbtq043")
        self.assertEqual((new["keyword"], new["category"], new["must_include"], new["exclude"], new["active"]),
                         ("セイコー SBTQ043", "watch", "SBTQ043", "ベルト|バンド", "Y"))
        self.assertEqual(new["name"], "세이코 SBTQ043 (검색)")
        self.assertIn(W.FROM_SEARCH, new["memo"])
        raw = (self.tmp / "catalog" / "products.csv").read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))           # 엑셀용 BOM
        self.assertEqual(raw.count(b"\r\n"), raw.count(b"\n"))     # CRLF 통일
        self.assertEqual(W.add_watch(self.tmp, SEARCH)[0], 409)   # 같은 검색어 두 번은 거절
        self.assertNotIn('data-act="add"', W.render(self.tmp))   # 추가한 뒤에는 '이미 감시 중'
        self.assertIn("직접 검색에서", W.render(self.tmp))

    def test_existing_keyword_is_rejected(self):
        dup = {**SEARCH, "query": {**SEARCH["query"], "keyword": "セイコー SBTQ041"}}
        self.assertEqual(W.add_watch(self.tmp, dup)[0], 409)           # products.csv 의 SBTQ041 과 같은 검색어

    def test_add_watch_needs_search(self):
        self.assertEqual(W.add_watch(self.tmp, {})[0], 400)

    def test_toggle(self):
        self.assertEqual(W.toggle_watch(self.tmp, "SBTQ041"), (200, {"id": "SBTQ041", "active": "N"}))
        self.assertEqual(W.toggle_watch(self.tmp, "SBTQ041")[1]["active"], "Y")
        self.assertEqual(W.toggle_watch(self.tmp, "nope")[0], 404)

    def test_make_id(self):
        self.assertEqual(W.make_id("セイコー SBTQ041", ["SBTQ041"], set()), "sbtq041")
        self.assertEqual(W.make_id("x", ["SBTQ041"], {"sbtq041"}), "sbtq041-2")
        self.assertTrue(W.make_id("セイコー", [], set()).startswith("item-"))

    # ---- 실행 명령
    def test_job_command(self):
        cmd = W.job_command("search", ("--evil x", "watch", "rakuten"))
        self.assertEqual(cmd[1:], ["main.py", "--search=--evil x", "--category=watch",
                                   "--market=rakuten"])   # 옵션으로 오인 안 됨
        self.assertEqual(W.job_command("run")[1:], ["main.py", "--no-alert"])

    def test_rates_edit_and_stale_banner(self):
        import tomllib
        from datetime import date
        page = W.render(self.tmp)
        self.assertIn("배대지 요금 확인 기록 없음", page)                   # JP = "" → 경고 배너
        self.assertIn('data-origin="JP"', page)
        st = self.tmp / "config" / "settings.toml"
        before = st.read_bytes()
        self.assertEqual(W.rates_request({"origin": "JP", "small": "7000", "medium": "9800", "large": "15800"}, self.tmp)[0], 200)
        cfg = tomllib.loads(st.read_text(encoding="utf-8"))
        self.assertEqual({k: cfg["shipping_profiles"]["JP"][k] for k in ("small", "medium", "large")},
                         {"small": 7000, "medium": 9800, "large": 15800})
        self.assertEqual(cfg["shipping_checked"]["JP"], date.today())
        self.assertEqual(cfg["shipping_profiles"]["US"], tomllib.loads(before.decode("utf-8"))["shipping_profiles"]["US"])
        crlf = bytes([13, 10])
        self.assertEqual(crlf in st.read_bytes(), crlf in before)              # 줄바꿈 형식 유지
        self.assertIn("~0.5kg", st.read_text(encoding="utf-8"))                # 주석 유지
        self.assertNotIn("배대지 요금 확인 기록 없음", W.render(self.tmp))
        for bad in ({"origin": "JP", "small": "x", "medium": "1", "large": "2"},
                    {"origin": "JP", "small": "9000", "medium": "100", "large": "200"},     # 순서 틀림
                    {"origin": "JP", "small": "1", "medium": "2", "large": "9999999"},
                    {"origin": "US", "small": "1", "medium": "2", "large": "3"},            # [shipping_checked] 에 없음
                    {"origin": "JP] x", "small": "1", "medium": "2", "large": "3"}):
            self.assertEqual(W.rates_request(bad, self.tmp)[0], 400, bad)
        self.assertEqual(tomllib.loads(st.read_text(encoding="utf-8"))["shipping_profiles"]["JP"]["small"], 7000)

    def test_add_watch_must_words(self):
        q = {"keyword": "セイコー セレクション クロノグラフ SBTR シリーズ", "category": "watch", "must_include": [],
             "exclude": [], "market": "rakuten"}
        self.assertEqual(W.suggest_must(q), "SBTR")                          # 숫자 없는 시리즈 코드도 제안
        self.assertEqual(W.suggest_must({**q, "must_include": ["SBTQ041"]}), "SBTQ041")
        self.assertEqual(W.suggest_must({"keyword": "ちいかわ ノート"}), "")
        code, out = W.add_watch(self.tmp, {"query": q}, "세이코 SBTR", " SBTR | クロノグラフ ")
        self.assertEqual((code, out["must_include"]), (200, ["SBTR", "クロノグラフ"]))
        rows = list(csv.DictReader(io.StringIO((self.tmp / "catalog" / "products.csv").read_text(encoding="utf-8-sig"))))
        self.assertEqual(next(r for r in rows if r["id"] == out["id"])["must_include"], "SBTR|クロノグラフ")
        self.assertEqual(W.add_watch(self.tmp, {"query": {**q, "keyword": "x y"}}, "", "|".join("abcdef"))[0], 400)
        self.assertIn('data-must="', W.render(self.tmp))

    def test_pause_all(self):
        saved = W.schedule_status
        W.schedule_status = lambda: {"registered": False, "next": ""}
        self.addCleanup(lambda: setattr(W, "schedule_status", saved))
        page = W.render(self.tmp)
        self.assertIn('data-act="pause" data-paused="1"', page)
        self.assertIn("등록 안 됨", page)
        self.assertEqual(W.pause_request({"paused": "yes"}, self.tmp)[0], 400)
        self.assertEqual(W.pause_request({"paused": True}, self.tmp), (200, {"paused": True}))
        page = W.render(self.tmp)
        self.assertIn("감시 전체 멈춤 중", page)
        self.assertIn('data-paused="0"', page)
        W.schedule_status = lambda: {"registered": True, "next": "2026-11-19 오후 4:00:00"}
        self.assertIn("매시간", W.render(self.tmp))
        self.assertEqual(W.pause_request({"paused": False}, self.tmp)[0], 200)
        self.assertNotIn("감시 전체 멈춤 중", W.render(self.tmp))

    def test_cost_cell_shows_forwarding(self):
        it = {"cost_krw": 183117, "forwarding_krw": 15000, "shipping_profile": "medium",
              "best_offer": {"shipping": 0, "shipping_known": True, "currency": "JPY"}}
        cell = W.cost_cell(it)
        self.assertIn("183,117원", cell)
        self.assertIn("배대지 15,000원 포함 (medium)", cell)
        self.assertIn("현지배송 무료", cell)                                   # 현지 무료여도 배대지 비용 표시
        it2 = {**it, "forwarding_krw": 0, "best_offer": {"shipping": None, "shipping_known": False}}
        self.assertIn("배대지 없음 (직배송)", W.cost_cell(it2))
        self.assertIn("현지배송 미확인", W.cost_cell(it2))
        self.assertEqual(W.cost_cell({"cost_krw": None}), W.money(None))

    def test_search_request_validation(self):
        self.assertEqual(W.search_request({"query": " ", "category": "watch"}, self.tmp)[0], 400)
        self.assertEqual(W.search_request({"query": "x" * 101, "category": "watch"}, self.tmp)[0], 400)
        self.assertEqual(W.search_request({"query": "x", "category": "cosmetics"}, self.tmp)[0], 400)   # 등록 금지
        self.assertEqual(W.search_request({"query": "x", "category": "nope"}, self.tmp)[0], 400)
        self.assertEqual(W.search_request({"query": "x"}, self.tmp)[0], 400)                     # 카테고리 안 고름

    def test_category_not_preselected(self):
        page = W.render(self.tmp)       # 지난 검색(시계)이 있어도 카테고리는 미리 골라 두지 않음
        sel = page.split('<select name="category"')[1].split("</select>")[0]
        self.assertIn('<option value="" selected disabled>카테고리 선택</option>', sel)
        self.assertEqual(sel.count("selected"), 1)
        self.assertIn(" required", sel.split(">")[0])
        self.assertEqual(W.search_request({"query": "x", "category": "watch", "market": "taobao"}, self.tmp)[0], 400)

    def test_add_watch_aliexpress_sets_source(self):
        ali = {**SEARCH, "query": {**SEARCH["query"], "keyword": "Seiko SBTQ043", "market": "aliexpress",
                                   "exclude": []}}
        self.assertEqual(W.add_watch(self.tmp, ali)[0], 200)
        new = self.products()[-1]
        self.assertEqual((new["keyword"], new["sources"]), ("Seiko SBTQ043", "aliexpress"))

    def test_market_select_rendered(self):
        (self.tmp / "reports").mkdir(exist_ok=True)
        q = {**SEARCH, "query": {**SEARCH["query"], "market": "aliexpress"}}
        (self.tmp / "reports" / "search.json").write_text(json.dumps(q), encoding="utf-8")
        page = W.render(self.tmp)
        self.assertIn('<select name="market"', page)
        self.assertIn('<option value="aliexpress" selected>', page)

    # ---- 서버
    def _server(self, fake_cmd=None):
        if fake_cmd:
            saved = W.job_command
            W.job_command = fake_cmd
            self.addCleanup(lambda: setattr(W, "job_command", saved))
        srv = W.make_server(0, self.tmp)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (srv.shutdown(), srv.server_close()))
        return f"http://127.0.0.1:{srv.server_address[1]}"

    def call(self, url, body=None, headers=None):
        h = {"Content-Type": "application/json", "X-Buyagent": "1"} if body is not None else {}
        h.update(headers or {})
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read().decode()
                return r.status, (json.loads(raw) if "json" in r.headers.get("Content-Type", "") else raw)
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"{}")

    def test_server_security(self):
        url = self._server()
        self.assertEqual(self.call(url + "/")[0], 200)
        self.assertEqual(self.call(url + "/api/toggle", {"id": "SBTQ041"}, {"X-Buyagent": "0"})[0], 403)
        req = urllib.request.Request(url + "/", headers={"Host": "evil.example:80"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 403)

    def test_server_search_job(self):
        url = self._server(lambda kind, arg=(): [sys.executable, "-c", "print('검색 끝')"])
        code, job = self.call(url + "/api/search", {"query": "セイコー  SBTQ041", "category": "watch"})
        self.assertEqual(code, 200)
        self.assertIn("セイコー SBTQ041", job["label"])
        for _ in range(100):
            j = self.call(url + "/api/job")[1]
            if j["status"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(j["status"], "done")
        self.assertIn("검색 끝", j["lines"])

    def test_one_job_at_a_time(self):
        url = self._server(lambda kind, arg=(): [sys.executable, "-c", "import time; time.sleep(2)"])
        self.assertEqual(self.call(url + "/api/run", {})[0], 200)
        self.assertEqual(self.call(url + "/api/search", {"query": "x", "category": "watch"})[0], 409)


if __name__ == "__main__":
    unittest.main()
