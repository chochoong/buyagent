"""새 쇼핑몰 플러그인 템플릿

사용법
  1) 이 파일을 복사해서 이름을 바꿈 (예: sources/myshop.py)  ← '_'로 시작하면 무시됨
  2) 아래 TODO를 채움
  3) config/settings.toml 에 섹션 추가:
        [sources.myshop]
        enabled = true
        api_key = "..."
        default_shipping = 10        # 현지 배송비 모를 때 가정값 (현지 통화)
  4) 해당 국가가 처음이면 settings.toml 에도 추가:
        [origins.US]  duty_free_usd = 200
        [shipping_profiles.US]  small = 20000 / medium = 30000 / large = 50000
        [fx.manual_krw]  USD = 1400

원칙
  - 공식 API나 제휴 피드만 사용하세요. 크롤링은 약관 위반이 될 수 있습니다.
  - 응답을 반드시 Offer 형식으로 변환해서 반환하세요. 나머지(필터·계산·알림)는 공통 코드가 처리합니다.
"""
from __future__ import annotations

from core.models import Offer, Query

from .base import Source, register


@register
class MyShop(Source):
    name = "myshop"              # TODO: settings.toml의 [sources.<name>] 과 동일하게
    currency = "USD"             # TODO
    origin = "US"                # TODO
    required_keys = ("api_key",)

    def search(self, q: Query) -> list[Offer]:
        d = self.get("https://api.example.com/search", {   # TODO: 실제 API 주소/파라미터
            "key": self.conf["api_key"], "q": q.jan or q.keyword,
            "sort": "newest" if q.newest else "price_asc",
        })
        return [
            Offer(
                source=self.name, key=f"my:{it['id']}", name=it["title"],
                price=float(it["price"]), currency=self.currency, origin=self.origin,
                shipping=float(it.get("shipping", self.default_shipping())),
                shipping_known="shipping" in it, in_stock=it.get("in_stock", True),
                shop=it.get("seller", ""), url=it["url"], jan=it.get("gtin", ""),
            )
            for it in d.get("items", [])
        ]
