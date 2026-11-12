"""라쿠텐 시장 (일본) - 공식 Ichiba Item Search API"""
from __future__ import annotations

from core.models import Offer, Query

from .base import Source, register


@register
class Rakuten(Source):
    name = "rakuten"
    currency = "JPY"
    origin = "JP"
    required_keys = ("application_id", "access_key")

    def search(self, q: Query) -> list[Offer]:
        d = self.get(self.conf["endpoint"], {
            "applicationId": self.conf["application_id"], "accessKey": self.conf["access_key"],
            "keyword": q.jan or q.keyword,
            "availability": 0,                 # 품절도 받아서 재입고 감지에 사용
            "sort": "-updateTimestamp" if q.newest else "+itemPrice",
            "hits": self.conf.get("hits", 30), "format": "json", "formatVersion": 2,
        })
        out = []
        for it in d.get("Items", []):
            free = it.get("postageFlag", 1) == 0           # 0 = 送料込み
            out.append(Offer(
                source=self.name, key=f"rk:{it.get('itemCode', '')}", name=it.get("itemName", ""),
                price=float(it.get("itemPrice", 0)), currency=self.currency, origin=self.origin,
                shipping=0 if free else self.default_shipping(), shipping_known=free,
                in_stock=it.get("availability", 1) == 1, shop=it.get("shopName", ""),
                url=it.get("itemUrl", ""), text=it.get("itemCaption", ""),
            ))
        return out
