"""알리익스프레스 (중국) - 공식 제휴 API 키워드 검색 (aliexpress.affiliate.product.query)

키 (비밀값 → 코드·settings.toml 이 아니라 buyagent/.env 에만):
  ALIEXPRESS_APP_KEY / ALIEXPRESS_APP_SECRET  ← AliExpress Open Platform 앱 (Affiliate 종류)
  ALIEXPRESS_TRACKING_ID                       ← AliExpress Portals 제휴 계정의 Tracking ID
키가 하나라도 없으면 이 소스는 꺼진 것으로 처리 (--search --market aliexpress 는 "키 없음" 실패)

※ 2026-11-18 기준 키가 아직 없어 실제 응답으로 검증하지 못함. 키를 받으면 아래를 공식 문서로 확인할 것
  - 게이트웨이 주소(endpoint)와 서명 방식(sign_method): 지금은 Open Platform 새 게이트웨이(IOP) 방식
    = 파라미터를 이름순으로 '이름+값' 이어 붙여 App Secret 으로 HMAC-SHA256 → 대문자 16진수
  - 응답 필드 이름 (parse_products 는 자료에서 확인한 이름 + 몇 가지 대안을 함께 봄)
  - 제휴 API 를 가격 조회 용도로 써도 되는지 약관
봇 차단 우회 없음: 공식 API 만 호출. 웹 페이지는 읽지 않음
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from pathlib import Path

from core.models import Offer, Query
from core.translate import load_dotenv

from .base import Source, register

BASE = Path(__file__).resolve().parent.parent
METHOD = "aliexpress.affiliate.product.query"
ENV_KEYS = ("ALIEXPRESS_APP_KEY", "ALIEXPRESS_APP_SECRET", "ALIEXPRESS_TRACKING_ID")


def sign(params: dict, secret: str) -> str:
    """IOP 서명: 이름순 '이름+값' → HMAC-SHA256(App Secret) → 대문자 hex"""
    text = "".join(f"{k}{params[k]}" for k in sorted(params) if k != "sign")
    return hmac.new(secret.encode(), text.encode(), hashlib.sha256).hexdigest().upper()


def _price(v) -> float:
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return 0.0


def parse_products(data: dict) -> list[dict]:
    """API 응답 → 상품 dict 목록. 오류 응답이면 RuntimeError (조용히 0건 처리하지 않음)"""
    if not isinstance(data, dict):
        raise RuntimeError("알리 응답 형식이 이상함")
    if "error_response" in data:
        e = data["error_response"] or {}
        raise RuntimeError(f"알리 API 오류 {e.get('code', '')}: {e.get('msg') or e.get('sub_msg') or ''}".strip())
    if data.get("code") not in (None, "0", 0):          # IOP 게이트웨이 형식의 오류
        raise RuntimeError(f"알리 API 오류 {data.get('code')}: {data.get('message', '')}")
    body = data.get("aliexpress_affiliate_product_query_response") or {}
    rr = body.get("resp_result") or {}
    code = rr.get("resp_code")
    if code not in (None, 200, "200"):
        if str(code) == "405":                          # 결과 없음으로 알려진 코드
            return []
        raise RuntimeError(f"알리 API 오류 {code}: {rr.get('resp_msg', '')}")
    result = rr.get("result") or {}
    prods = (result.get("products") or {})
    prods = prods.get("product", []) if isinstance(prods, dict) else prods
    out = []
    for p in prods or []:
        pid = str(p.get("product_id") or "").strip()
        if not pid:
            continue
        price = _price(p.get("target_sale_price") or p.get("sale_price"))
        out.append({
            "id": pid,
            "name": str(p.get("product_title") or "").strip(),
            "price": price,
            "currency": str(p.get("target_sale_price_currency") or p.get("sale_price_currency") or "USD"),
            "original": _price(p.get("target_original_price") or p.get("original_price")),
            "shop": str(p.get("shop_name") or (f"Shop {p['shop_id']}" if p.get("shop_id") else "")).strip(),
            "url": str(p.get("product_detail_url") or "").split("?")[0],
        })
    return out


@register
class AliExpress(Source):
    name = "aliexpress"
    currency = "USD"
    origin = "CN_DIRECT"        # 알리가 한국까지 직접 배송 → 중국 배대지 요금표(CN) 대신 요금 0 표 (settings.toml)
    env_keys = ENV_KEYS
    supports_newest = False     # 신상 발견에는 안 씀
    explicit_only = True        # 일본어 감시 검색어로 알리를 뒤지지 않음 → --market aliexpress 또는 sources 에 적은 상품만

    def keys(self) -> dict[str, str]:
        load_dotenv(BASE / ".env")
        return {k: os.environ.get(k, "").strip() for k in ENV_KEYS}

    def ready(self) -> bool:
        return bool(self.conf.get("enabled")) and all(self.keys().values())

    def request(self, params: dict) -> dict:
        """실제 호출 (테스트에서는 이 함수를 가짜로 바꿈)"""
        return self.get(self.conf.get("endpoint", "https://api-sg.aliexpress.com/sync"), params)

    def search(self, q: Query) -> list[Offer]:
        k = self.keys()
        params = {
            "method": METHOD,
            "app_key": k["ALIEXPRESS_APP_KEY"],
            "sign_method": "sha256",
            "timestamp": str(int(time.time() * 1000)),
            "keywords": q.keyword,
            "target_currency": self.currency,
            "target_language": "EN",
            "ship_to_country": self.conf.get("ship_to", "KR"),
            "page_size": str(int(self.conf.get("page_size", 40))),
            "page_no": "1",
            "tracking_id": k["ALIEXPRESS_TRACKING_ID"],
        }
        params["sign"] = sign(params, k["ALIEXPRESS_APP_SECRET"])
        items = parse_products(self.request(params))
        out = []
        for it in items:
            if it["currency"] != self.currency or it["price"] <= 0:
                continue
            out.append(Offer(
                source=self.name, key=f"ali:{it['id']}", name=it["name"], price=it["price"],
                currency=self.currency, origin=self.origin,
                shipping=float(self.conf.get("default_shipping", 0)), shipping_known=False,
                in_stock=True, shop=it["shop"], url=it["url"], text=it["name"],
            ))
        return out
