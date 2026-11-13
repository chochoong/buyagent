"""네이버 오픈 API: 쇼핑 검색(국내 판매가·경쟁) + 데이터랩 검색어 트렌드

키 발급: https://developers.naver.com → Application → 애플리케이션 등록
  - 사용 API: '검색' (쇼핑 검색), '데이터랩 (검색어트렌드)'
  - Client ID / Client Secret → config/settings.local.toml 의 [naver]

쇼핑 검색은 하루 25,000회까지 무료.
"""
from __future__ import annotations

import html
import re
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta

from .http import get_json, post_json
from .rules import norm

TAG = re.compile(r"<[^>]+>")


@dataclass
class ShopItem:
    title: str
    price: int
    mall: str
    link: str = ""
    product_type: int = 1         # 1~3 일반, 4~6 중고, 7~9 단종, 10~12 판매예정
    brand: str = ""
    category: str = ""


@dataclass
class Market:
    """키워드 하나의 국내 시장 요약"""
    query: str
    total: int = 0                # 네이버 쇼핑 전체 상품 수 (국내, 중고·렌탈 제외)
    total_with_cb: int = 0        # 해외직구 포함 상품 수
    items: list[ShopItem] = field(default_factory=list)   # 조건에 맞는 상품 (정확도순 상위)
    excluded: int = 0             # 필수·제외 단어로 걸러진 수

    @property
    def prices(self) -> list[int]:
        return sorted(i.price for i in self.items if i.price > 0)

    def stat(self, how: str) -> int:
        p = self.prices
        if not p:
            return 0
        if how == "min":
            return p[0]
        if how == "median":
            return int(statistics.median(p))
        if how == "max":
            return p[-1]
        return p[int((len(p) - 1) * 0.25)] if how == "p25" else p[0]

    @property
    def malls(self) -> int:
        return len({i.mall for i in self.items if i.mall})

    @property
    def cb_share(self) -> float:
        """해외직구(구매대행) 상품 비율"""
        return 1 - self.total / self.total_with_cb if self.total_with_cb else 0.0

    def summary(self) -> str:
        if not self.items:
            return f"네이버 '{self.query}': 조건에 맞는 상품 없음 (필수·제외 단어 확인)"
        return (f"네이버 '{self.query}': 최저 {self.stat('min'):,} · 하위25% {self.stat('p25'):,} · "
                f"중간 {self.stat('median'):,}원 | 상품 {self.total:,}개 · 상위 {len(self.items)}개 중 "
                f"판매처 {self.malls}곳 · 해외직구 {self.cb_share:.0%}")


def clean_title(t: str) -> str:
    return html.unescape(TAG.sub("", t or "")).strip()


class Naver:
    def __init__(self, conf: dict):
        self.conf = conf
        self.base = conf.get("api_base", "https://openapi.naver.com").rstrip("/")

    def ready(self) -> bool:
        return bool(self.conf.get("client_id") and self.conf.get("client_secret"))

    def _headers(self) -> dict:
        return {"X-Naver-Client-Id": self.conf["client_id"],
                "X-Naver-Client-Secret": self.conf["client_secret"]}

    def _shop(self, query: str, display: int, sort: str, exclude: str) -> dict:
        params = {"query": query, "display": display, "start": 1, "sort": sort}
        if exclude:
            params["exclude"] = exclude
        return get_json(f"{self.base}/v1/search/shop.json", params, self._headers(),
                        min_interval=float(self.conf.get("min_interval", 0.2)),
                        retries=int(self.conf.get("retries", 2)))

    def market(self, query: str, must: list[str] | None = None, exclude_words: list[str] | None = None,
               display: int = 0, with_cb: bool = True) -> Market:
        """정확도순 상위 상품으로 국내 판매가 분포를 만듦 (중고·렌탈·해외직구 제외)"""
        display = display or int(self.conf.get("display", 40))
        d = self._shop(query, display, "sim", "used:rental:cbshop")
        m = Market(query=query, total=int(d.get("total", 0)))
        must_n = [norm(w) for w in (must or [])]
        excl_n = [norm(w) for w in (exclude_words or [])]
        for it in d.get("items", []):
            title = clean_title(it.get("title", ""))
            n = norm(title)
            if any(w not in n for w in must_n) or any(w in n for w in excl_n):
                m.excluded += 1
                continue
            try:
                ptype = int(it.get("productType", 1))
            except ValueError:
                ptype = 1
            if ptype > 3:              # 중고·단종·판매예정
                m.excluded += 1
                continue
            m.items.append(ShopItem(
                title=title, price=int(float(it.get("lprice") or 0)), mall=it.get("mallName", ""),
                link=it.get("link", ""), product_type=ptype, brand=it.get("brand", ""),
                category=" > ".join(x for x in (it.get(f"category{i}", "") for i in range(1, 5)) if x)))
        if with_cb:
            m.total_with_cb = int(self._shop(query, 1, "sim", "used:rental").get("total", 0))
        return m

    def trend(self, keywords: list[str], months: int = 12) -> dict[str, list[float]]:
        """데이터랩 검색어 트렌드: 키워드별 월별 상대 검색량 (요청 안에서 최대값 = 100)"""
        end = date.today().replace(day=1) - timedelta(days=1)    # 이번 달은 덜 끝났으니 지난달까지
        y, mth = end.year, end.month - months + 1
        while mth <= 0:
            y, mth = y - 1, mth + 12
        out: dict[str, list[float]] = {}
        for i in range(0, len(keywords), 5):           # 한 번에 최대 5개
            body = {"startDate": f"{y}-{mth:02d}-01", "endDate": end.isoformat(),
                    "timeUnit": "month",
                    "keywordGroups": [{"groupName": k, "keywords": [k]} for k in keywords[i:i + 5]]}
            d = post_json(f"{self.base}/v1/datalab/search", body, self._headers())
            for r in d.get("results", []):
                out[r["title"]] = [float(x.get("ratio", 0)) for x in r.get("data", [])]
        return out


def trend_change(series: list[float]) -> float | None:
    """최근 3개월 평균 ÷ 그 전 3개월 평균 - 1"""
    if len(series) < 6:
        return None
    prev, last = sum(series[-6:-3]) / 3, sum(series[-3:]) / 3
    return (last / prev - 1) if prev else None
