"""알리익스프레스 (중국) - 공식 Open Platform 드롭쉬핑 상품 검색 (aliexpress.ds.text.search)

키 (비밀값 → 코드·settings.toml 이 아니라 buyagent/.env 에만):
  ALIEXPRESS_APP_KEY / ALIEXPRESS_APP_SECRET  ← AliExpress Open Platform 앱
토큰: buyagent/data/aliexpress_token.json (data/get_token.py 로 처음 발급, 이후 core/ali_token.py 가 자동 갱신)

2026-11-24 실제 키·토큰으로 확인:
  - 제휴 API(aliexpress.affiliate.product.query)는 이 앱에 권한 없음(InsufficientPermission) → 드롭쉬핑 검색 사용
  - 게이트웨이 https://api-sg.aliexpress.com/sync, 서명 = 이름순 '이름+값' HMAC-SHA256(App Secret) 대문자 hex
  - 응답: aliexpress_ds_text_search_response.code "00", data.products.selection_search_product[]
          itemId · title · targetSalePrice(USD) · targetOriginalPrice · itemUrl("//www.aliexpress.com/item/…?appkey=…")
          orders · score · evaluateRate (판매처 이름은 안 옴)
상품 조회에만 씀 (주문 API 안 부름). 봇 차단 우회 없음: 공식 API 만 호출. 웹 페이지는 읽지 않음
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from pathlib import Path

from core import ali_token
from core.models import Offer, Query
from core.translate import load_dotenv

from .base import Source, register

BASE = Path(__file__).resolve().parent.parent
METHOD = "aliexpress.ds.text.search"
ENV_KEYS = ("ALIEXPRESS_APP_KEY", "ALIEXPRESS_APP_SECRET")


def sign(params: dict, secret: str) -> str:
    """IOP 서명: 이름순 '이름+값' → HMAC-SHA256(App Secret) → 대문자 hex"""
    text = "".join(f"{k}{params[k]}" for k in sorted(params) if k != "sign")
    return hmac.new(secret.encode(), text.encode(), hashlib.sha256).hexdigest().upper()


def _price(v) -> float:
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return 0.0


def _url(u) -> str:
    u = str(u or "").split("?")[0].strip()          # ?appkey=… 등 추적 파라미터 제거
    return "https:" + u if u.startswith("//") else u


def parse_products(data: dict) -> list[dict]:
    """API 응답 → 상품 dict 목록. 오류 응답이면 RuntimeError (조용히 0건 처리하지 않음)"""
    if not isinstance(data, dict):
        raise RuntimeError("알리 응답 형식이 이상함")
    if "error_response" in data:
        e = data["error_response"] or {}
        raise RuntimeError(f"알리 API 오류 {e.get('code', '')}: {e.get('msg') or e.get('sub_msg') or ''}".strip())
    if data.get("code") not in (None, "0", 0):          # IOP 게이트웨이 형식의 오류 (토큰 만료 등)
        raise RuntimeError(f"알리 API 오류 {data.get('code')}: {data.get('message', '')}")
    body = data.get("aliexpress_ds_text_search_response")
    if not isinstance(body, dict):
        raise RuntimeError("알리 응답에 검색 결과 칸이 없음")
    if str(body.get("code")) not in ("00", "0", "200"):
        raise RuntimeError(f"알리 API 오류 {body.get('code')}: {body.get('msg') or body.get('message') or ''}".strip())
    prods = ((body.get("data") or {}).get("products") or {})
    prods = prods.get("selection_search_product", []) if isinstance(prods, dict) else prods
    out = []
    for p in prods or []:
        pid = str(p.get("itemId") or "").strip()
        if not pid:
            continue
        cur = str(p.get("targetOriginalPriceCurrency") or "USD")
        out.append({
            "id": pid,
            "name": " ".join(str(p.get("title") or "").split()),
            "price": _price(p.get("targetSalePrice")),
            "currency": cur,
            "original": _price(p.get("targetOriginalPrice")),
            "shop": "AliExpress",
            "url": _url(p.get("itemUrl")) or f"https://www.aliexpress.com/item/{pid}.html",
            "orders": str(p.get("orders") or ""),
            "score": str(p.get("score") or ""),
        })
    return out


def shop_label(it: dict) -> str:
    """판매처 칸: 판매처 이름이 안 와서 주문 수·평점을 같이 (싼 미끼 상품 구별용)"""
    extra = " · ".join(x for x in (f"주문 {it['orders']}" if it.get("orders") else "",
                                   f"평점 {it['score']}" if it.get("score") else "") if x)
    return f"{it['shop']} ({extra})" if extra else it["shop"]


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

    def token(self, app_key: str, secret: str) -> str:
        """data/aliexpress_token.json 의 access_token (만료가 가까우면 갱신). 테스트에서는 바꿔 끼움"""
        return ali_token.access_token(BASE, app_key, secret)

    def request(self, params: dict) -> dict:
        """실제 호출 (테스트에서는 이 함수를 가짜로 바꿈)"""
        return self.get(self.conf.get("endpoint", "https://api-sg.aliexpress.com/sync"), params)

    def search(self, q: Query) -> list[Offer]:
        k = self.keys()
        try:
            session = self.token(k["ALIEXPRESS_APP_KEY"], k["ALIEXPRESS_APP_SECRET"])
        except ali_token.TokenError as err:
            raise RuntimeError(str(err)) from err
        params = {
            "method": METHOD,
            "app_key": k["ALIEXPRESS_APP_KEY"],
            "session": session,
            "sign_method": "sha256",
            "timestamp": str(int(time.time() * 1000)),
            "keyWord": q.keyword,
            "local": "en_US",
            "countryCode": self.conf.get("ship_to", "KR"),
            "currency": self.currency,
            "pageSize": str(int(self.conf.get("page_size", 40))),
            "pageIndex": "1",
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
                in_stock=True, shop=shop_label(it), url=it["url"], text=it["name"],
            ))
        return out
