"""라쿠텐 시장 (일본) - 공식 Ichiba Item Search API

overseas_first = true 면 해외배송 상품을 먼저 한 번 더 조회 (shipOverseasFlag=1, shipOverseasArea=ship_overseas_area)
→ 그 결과에 나온 상품은 Offer.overseas="api" ('판매자 해외배송 가능', 원가는 직배송·배대지 중 싼 쪽)
※ 2026-11-24 API 키가 없어 두 파라미터의 실제 동작은 미검증 → 키를 받으면 응답으로 확인
"""
from __future__ import annotations

from core.models import Offer, Query

from .base import Source, register


@register
class Rakuten(Source):
    name = "rakuten"
    currency = "JPY"
    origin = "JP"
    required_keys = ("application_id",)
    recommended_keys = ("access_key",)   # 발급 화면에 없으면 비워 두고 --probe 로 확인

    def search(self, q: Query) -> list[Offer]:
        params = {
            "applicationId": self.conf["application_id"],
            "keyword": q.jan or q.keyword,
            "availability": 0,                 # 품절도 받아서 재입고 감지에 사용
            "sort": "-updateTimestamp" if q.newest else "+itemPrice",
            "hits": self.conf.get("hits", 30), "format": "json", "formatVersion": 2,
        }
        if self.conf.get("access_key"):
            params["accessKey"] = self.conf["access_key"]
        overseas: set[str] = set()
        first: list[dict] = []
        if self.conf.get("overseas_first") and not q.newest:     # 해외배송 상품 먼저 (신상 발견은 그대로)
            extra = {"shipOverseasFlag": 1}
            if self.conf.get("ship_overseas_area"):
                extra["shipOverseasArea"] = self.conf["ship_overseas_area"]
            first = self.get(self.conf["endpoint"], {**params, **extra}).get("Items", [])
            overseas = {it.get("itemCode", "") for it in first if it.get("itemCode")}
        d = self.get(self.conf["endpoint"], params)
        seen: set[str] = set()
        out = []
        for it in first + d.get("Items", []):
            code = it.get("itemCode", "")
            if code and code in seen:
                continue
            seen.add(code)
            free = it.get("postageFlag", 1) == 0           # 0 = 送料込み
            out.append(Offer(
                source=self.name, key=f"rk:{it.get('itemCode', '')}", name=it.get("itemName", ""),
                price=float(it.get("itemPrice", 0)), currency=self.currency, origin=self.origin,
                shipping=0 if free else self.default_shipping(), shipping_known=free,
                in_stock=it.get("availability", 1) == 1, shop=it.get("shopName", ""),
                url=it.get("itemUrl", ""), text=it.get("itemCaption", ""),
                overseas="api" if code in overseas else "",
            ))
        return out
