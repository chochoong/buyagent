"""web.py (buyagent 자체 화면) 테스트 — 실제 catalog·reports 는 건드리지 않고 임시 폴더에서"""
from __future__ import annotations

import csv
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
        cmd = W.job_command("search", ("--evil x", "watch"))
        self.assertEqual(cmd[1:], ["main.py", "--search=--evil x", "--category=watch"])   # 옵션으로 오인 안 됨
        self.assertEqual(W.job_command("run")[1:], ["main.py", "--no-alert"])

    def test_search_request_validation(self):
        self.assertEqual(W.search_request({"query": " ", "category": "watch"}, self.tmp)[0], 400)
        self.assertEqual(W.search_request({"query": "x" * 101, "category": "watch"}, self.tmp)[0], 400)
        self.assertEqual(W.search_request({"query": "x", "category": "cosmetics"}, self.tmp)[0], 400)   # 등록 금지
        self.assertEqual(W.search_request({"query": "x", "category": "nope"}, self.tmp)[0], 400)

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
