"""라쿠텐 시장 (일본) - 검색 페이지 수집 (API 키 없이)

라쿠텐 API 키를 못 받을 때 쓰는 대안입니다. PC에 설치된 크롬을 화면 없이(headless) 띄워
검색 결과 페이지(search.rakuten.co.jp)를 읽고, API 결과와 같은 Offer 형식으로 바꿉니다.
(일반 HTTP 요청은 라쿠텐 봇 차단에 막혀서 실제 크롬을 씀 → 추가 패키지 설치 불필요)

제약 (API와 다른 점)
- robots.txt 가 정렬(?s=) 파라미터를 금지 → '표준순' 결과만 읽음. 가격순·최신순 불가
  → 신상 발견(discover)에는 쓰지 않고, 상품 감시는 pages 를 늘려 후보를 넓힘
- 품절 상품은 검색 결과에 거의 안 나옴 → 품절은 '목록에서 사라짐'으로 감지됨
- 페이지 구조가 바뀌면 0건이 나옴 → '검색 결과 0건' 오류로 알림 (조용히 품절 처리하지 않음)
- 호출마다 크롬을 띄우므로 느림(검색 1건에 수 초). min_interval 로 간격을 둠
"""
from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path

from core.models import Offer, Query

from .base import Source, register

SEARCH_URL = "https://search.rakuten.co.jp/search/mall/{kw}/"
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]
CARD_SPLIT = re.compile(r'<div class="dui-card searchresultitem[^"]*"')
_last_call = 0.0


def find_chrome(conf_path: str = "") -> str:
    if conf_path:
        return conf_path if Path(conf_path).exists() else ""
    for c in CHROME_CANDIDATES:
        if c and Path(c).exists():
            return c
    for name in ("chrome", "google-chrome", "chromium", "msedge"):
        p = shutil.which(name)
        if p:
            return p
    return ""


def _attr(block: str, name: str) -> str:
    m = re.search(rf'{name}="([^"]*)"', block)
    return html.unescape(m.group(1)) if m else ""


def _text(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def parse_search_html(page: str) -> list[dict]:
    """검색 결과 HTML → 상품 dict 목록 (테스트하기 쉽게 크롬 실행과 분리)"""
    items, seen = [], set()
    for block in CARD_SPLIT.split(page)[1:]:
        head = block[:block.find(">")]               # 카드 div 의 속성 부분
        shop_id, item_id = _attr(head, "data-shop-id"), _attr(head, "data-id")
        if not (shop_id and item_id) or (shop_id, item_id) in seen:
            continue                                  # 광고(PR)로 같은 상품이 두 번 나오면 한 번만
        seen.add((shop_id, item_id))
        t = re.search(r'<a [^>]*data-link="item"[^>]*>(.*?)</a>', block, re.S) \
            or re.search(r'<h2[^>]*>(.*?)</h2>', block, re.S)
        name = _text(t.group(1)) if t else ""
        url_m = re.search(r'href="(https://item\.rakuten\.co\.jp/[^"?]+)', block)
        price_s = _attr(block, "data-price") or _attr(head, "data-track-price")
        max_s = _attr(block, "data-max-price")
        shop = ""
        sm = re.search(r'class="[^"]*\bmerchant\b[^"]*".*?<a [^>]*>(.*?)</a>', block, re.S)
        if sm:
            shop = _text(sm.group(1))
        try:
            price = float(price_s.replace(",", ""))
        except ValueError:
            price = 0.0
        items.append({
            "shop_id": shop_id, "item_id": item_id, "name": name, "price": price,
            "max_price": float(max_s) if max_s.replace(".", "").isdigit() else price,
            "free_shipping": "free-shipping-label" in block,
            "sold_out": "売り切れ" in block,
            "shop": shop, "url": url_m.group(1) if url_m else "",
        })
    return items


@register
class RakutenWeb(Source):
    name = "rakuten_web"
    currency = "JPY"
    origin = "JP"
    supports_newest = False     # 정렬 파라미터를 robots.txt 가 금지 → 신상 발견 불가

    def ready(self) -> bool:
        return bool(self.conf.get("enabled")) and bool(find_chrome(self.conf.get("chrome_path", "")))

    def fetch_html(self, url: str) -> str:
        global _last_call
        gap = float(self.conf.get("min_interval", 5)) - (time.monotonic() - _last_call)
        if gap > 0:
            time.sleep(gap)
        _last_call = time.monotonic()
        chrome = find_chrome(self.conf.get("chrome_path", ""))
        with tempfile.TemporaryDirectory(prefix="buyagent_chrome_") as prof:
            cmd = [chrome, "--headless=new", "--disable-gpu", "--no-first-run",
                   "--no-default-browser-check", "--disable-extensions", "--lang=ja",
                   f"--user-data-dir={prof}",
                   f"--virtual-time-budget={int(self.conf.get('wait_ms', 8000))}",
                   "--dump-dom", url]
            r = subprocess.run(cmd, capture_output=True, timeout=int(self.conf.get("timeout", 60)))
        return r.stdout.decode("utf-8", "replace")

    def search(self, q: Query) -> list[Offer]:
        kw = urllib.parse.quote(q.jan or q.keyword, safe="")
        retries = int(self.conf.get("retries", 1))
        out, seen = [], set()
        for page in range(1, max(1, int(self.conf.get("pages", 1))) + 1):
            url = SEARCH_URL.format(kw=kw) + (f"?p={page}" if page > 1 else "")
            for attempt in range(retries + 1):
                doc = self.fetch_html(url)
                items = parse_search_html(doc)
                if items or "searchresultitem" in doc or "見つかりませんでした" in doc:
                    break
            else:
                blocked = len(doc) < 2000
                raise RuntimeError("라쿠텐 검색 페이지 " + (
                    "접속 차단(봇 감지)" if blocked else "구조 변경으로 상품을 못 읽음 → sources/rakuten_web.py 수정 필요"))
            self.last_raw = {"url": url, "items": items}
            for it in items:
                key = f"rkw:{it['shop_id']}:{it['item_id']}"
                if key in seen:
                    continue
                seen.add(key)
                notes = []
                if it["max_price"] > it["price"]:
                    notes.append(f"옵션별 가격 차이 ({it['price']:,.0f}~{it['max_price']:,.0f}엔) → 최저 옵션 기준")
                free = it["free_shipping"]
                out.append(Offer(
                    source=self.name, key=key, name=it["name"], price=it["price"],
                    currency=self.currency, origin=self.origin,
                    shipping=0 if free else self.default_shipping(), shipping_known=free,
                    in_stock=not it["sold_out"], shop=it["shop"], url=it["url"],
                    text=it["name"], notes=notes,
                ))
            if len(items) < 40:       # 마지막 페이지
                break
        return out
