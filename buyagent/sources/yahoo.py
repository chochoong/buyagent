"""Yahoo! 쇼핑 (일본) - 商品検索 API v3"""
from __future__ import annotations

from core.models import Offer, Query

from .base import Source, register


@register
class YahooShopping(Source):
    name = "yahoo"
    currency = "JPY"
    origin = "JP"
    required_keys = ("app_id",)
    supports_newest = False     # 정렬은 -score/+price/-price/-review_count 뿐 → 신상 발견은 라쿠텐으로

    def search(self, q: Query) -> list[Offer]:
        params = {"appid": self.conf["app_id"], "results": self.conf.get("results", 30),
                  "sort": "+price", "condition": "new"}
        if q.jan:
            params["jan_code"] = q.jan
        else:
            params["query"] = q.keyword
        if self.conf.get("cross_border_only", True):
            params["is_cross_border_agency"] = "true"   # 해외 구매대행 허용 상품만
        d = self.get(self.conf["endpoint"], params)
        out = []
        for it in d.get("hits", []):
            seller = it.get("seller") or {}
            free = (it.get("shipping") or {}).get("code") == 2   # 2 = 送料無料
            out.append(Offer(
                source=self.name, key=f"yh:{seller.get('sellerId', '')}:{it.get('code', '')}",
                name=it.get("name", ""), price=float(it.get("price", 0)),
                currency=self.currency, origin=self.origin,
                shipping=0 if free else self.default_shipping(), shipping_known=free,
                in_stock=bool(it.get("inStock", True)), shop=seller.get("name", ""),
                url=it.get("url", ""), jan=it.get("janCode") or "",
                used=it.get("condition") == "used",
            ))
        return out
