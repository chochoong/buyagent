"""이베이 (미국) - 공식 Browse API 키워드 검색 (buy/browse/v1/item_summary/search)

키 (비밀값 → 코드·settings.toml 이 아니라 buyagent/.env 에만):
  EBAY_CLIENT_ID / EBAY_CLIENT_SECRET  ← eBay Developers Program (무료) → Production Keyset 의 App ID / Cert ID
키가 하나라도 없으면 이 소스는 꺼진 것으로 처리 (--search --market ebay 는 "키 없음" 실패)

방식
  - 키로 Application 토큰을 받음 (OAuth client credentials, 사용자 로그인 없음, 약 2시간 유효 → 실행 중 재사용)
  - 새 상품·즉시 구매·미국 내 판매자·미국 배송 가능한 것만 (filter). 중고·경매는 처음부터 제외
  - 미국 배대지로 받는 기준 (origin US → [shipping_profiles.US] 배대지 요금 더함)
    배대지까지 배송비는 forwarder_zip(배대지 우편번호)을 넣으면 그 주소 기준으로 eBay 가 계산해 줌
※ 2026-11-18 기준 키가 없어 실제 응답으로 검증하지 못함 (가짜 응답 테스트). 키를 받으면 응답 1건으로 필드 확인
봇 차단 우회 없음: 공식 API 만 호출
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from core.models import Offer, Query
from core.translate import load_dotenv

from .base import Source, register

BASE = Path(__file__).resolve().parent.parent
ENV_KEYS = ("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET")
SCOPE = "https://api.ebay.com/oauth/api_scope"


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def parse_items(data: dict) -> list[dict]:
    """검색 응답 → 상품 dict 목록. 오류 응답이면 RuntimeError (조용히 0건 처리하지 않음)"""
    if not isinstance(data, dict):
        raise RuntimeError("이베이 응답 형식이 이상함")
    if data.get("errors"):
        e = data["errors"][0] or {}
        raise RuntimeError(f"이베이 API 오류 {e.get('errorId', '')}: {e.get('message', '')}".strip())
    out = []
    for it in data.get("itemSummaries") or []:
        price = it.get("price") or {}
        ship = None                                     # None = 배송비 모름
        for o in it.get("shippingOptions") or []:
            cost = o.get("shippingCost")
            if cost and cost.get("currency") == price.get("currency"):
                c = _num(cost.get("value"))
                ship = c if ship is None else min(ship, c)
            elif (o.get("shippingCostType") or "").upper() == "FREE":
                ship = 0.0
        out.append({
            "id": str(it.get("itemId") or ""),
            "name": str(it.get("title") or "").strip(),
            "price": _num(price.get("value")),
            "currency": str(price.get("currency") or ""),
            "shipping": ship,
            "condition": str(it.get("condition") or ""),
            "shop": str((it.get("seller") or {}).get("username") or ""),
            "feedback": str((it.get("seller") or {}).get("feedbackPercentage") or ""),
            "url": str(it.get("itemWebUrl") or "").split("?")[0],
        })
    return [x for x in out if x["id"]]


@register
class Ebay(Source):
    name = "ebay"
    currency = "USD"
    origin = "US"
    env_keys = ENV_KEYS
    supports_newest = False
    explicit_only = True        # 일본어 감시 검색어로 이베이를 뒤지지 않음 → --market ebay 또는 sources 에 적은 상품만

    _token: tuple[str, float] = ("", 0.0)

    def keys(self) -> dict[str, str]:
        load_dotenv(BASE / ".env")
        return {k: os.environ.get(k, "").strip() for k in ENV_KEYS}

    def ready(self) -> bool:
        return bool(self.conf.get("enabled")) and all(self.keys().values())

    # -- 실제 호출 (테스트에서는 이 두 함수를 가짜로 바꿈) --
    def token_request(self, client_id: str, secret: str) -> dict:
        auth = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
        body = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": SCOPE}).encode()
        req = urllib.request.Request(self.conf.get("token_url", "https://api.ebay.com/identity/v1/oauth2/token"),
                                     data=body, method="POST",
                                     headers={"Authorization": f"Basic {auth}",
                                              "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode("utf-8"))

    def request(self, params: dict, headers: dict) -> dict:
        return self.get(self.conf.get("endpoint", "https://api.ebay.com/buy/browse/v1/item_summary/search"),
                        params, headers)

    def token(self) -> str:
        tok, until = Ebay._token
        if tok and time.time() < until - 60:
            return tok
        k = self.keys()
        try:
            d = self.token_request(k["EBAY_CLIENT_ID"], k["EBAY_CLIENT_SECRET"])
        except urllib.error.HTTPError as e:     # eBay 가 준 이유(error_description)를 그대로 보여 줌
            try:
                why = json.loads(e.read().decode("utf-8", "replace")).get("error_description", "")
            except (ValueError, OSError, AttributeError):
                why = ""
            hint = (" → developer.ebay.com 에서 Production 키셋이 켜져 있는지(계정 삭제 알림 설정/면제 완료), "
                    "App ID·Cert ID 를 정확히 복사했는지 확인" if e.code == 401 else "")
            raise RuntimeError(f"이베이 토큰 발급 실패 HTTP {e.code} {why}{hint}".strip()) from e
        except Exception as e:
            raise RuntimeError(f"이베이 토큰 발급 실패 ({type(e).__name__}) → .env 의 EBAY_CLIENT_ID/SECRET 확인") from e
        tok = str(d.get("access_token") or "")
        if not tok:
            raise RuntimeError(f"이베이 토큰 발급 실패: {d.get('error_description') or d.get('error') or '응답에 토큰 없음'}")
        Ebay._token = (tok, time.time() + float(d.get("expires_in") or 7200))
        return tok

    def search(self, q: Query) -> list[Offer]:
        filt = "conditions:{NEW},buyingOptions:{FIXED_PRICE},itemLocationCountry:US,deliveryCountry:US," \
               f"priceCurrency:{self.currency}"
        params = {"q": q.keyword, "filter": filt, "limit": str(int(self.conf.get("limit", 50)))}
        headers = {"Authorization": f"Bearer {self.token()}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"}
        zip_code = str(self.conf.get("forwarder_zip", "")).strip()
        if zip_code:            # 배대지 주소 기준 배송비
            headers["X-EBAY-C-ENDUSERCTX"] = "contextualLocation=" + urllib.parse.quote(f"country=US,zip={zip_code}")
        out = []
        for it in parse_items(self.request(params, headers)):
            if it["currency"] != self.currency or it["price"] <= 0:
                continue
            # 배송비는 배대지 우편번호를 넣었을 때만 '확인됨' (무료배송은 주소와 상관없이 확인됨)
            known = it["shipping"] is not None and (bool(zip_code) or it["shipping"] == 0)
            notes = []
            if it["feedback"]:
                try:
                    if float(it["feedback"]) < float(self.conf.get("min_feedback", 98)):
                        notes.append(f"판매자 평가 {it['feedback']}% → 판매자 확인")
                except ValueError:
                    pass
            out.append(Offer(
                source=self.name, key=f"ebay:{it['id']}", name=it["name"], price=it["price"],
                currency=self.currency, origin=self.origin,
                # 배송비를 이베이가 알려 주면 (주소 기준이 아니어도) 그 값을 가정값으로 씀
                shipping=it["shipping"] if it["shipping"] is not None else self.default_shipping(),
                shipping_known=known,
                in_stock=True, shop=it["shop"], url=it["url"], text=it["name"], notes=notes,
            ))
        return out
