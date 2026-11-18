"""수동 가격 소스: API가 없는 쇼핑몰(타오바오·1688·알리 등)의 가격을 직접 입력

catalog/manual_prices.csv 에 products.csv 의 id 별로 가격을 적어 두면
다른 쇼핑몰 검색 결과처럼 원가·권장가·마진 계산, 가격 변동 알림이 그대로 적용됩니다.

  id,price,currency,origin,shipping,in_stock,shop,url,checked,memo
  akko-5075b,329,CNY,CN,0,Y,Akko 타오바오 공식점,https://...,2026-11-13,

- origin   : CN = 중국 배대지 거침(배대지 요금 더함), CN_DIRECT = 알리처럼 한국까지 직배송(배대지 요금 0)
- price    : 현지가 (해당 통화)
- shipping : 현지 배송비. 0 = 배대지까지 무료배송 확인됨. 비우면 "미확인" → 0으로 가정하고 "확인 후 shipping 입력" 알림
- in_stock : N 이면 품절로 처리 (품절·재입고 알림)
- checked  : 가격을 마지막으로 확인한 날짜. [sources.manual] stale_days 보다 오래되면 '가격 재확인' 알림
- 한 상품에 여러 줄(여러 판매처)을 적으면 그중 원가가 가장 낮은 곳을 고릅니다.
"""
from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path

from core.models import Offer, Query

from .base import Source, register

BASE = Path(__file__).resolve().parent.parent


def _num(v: str | None) -> float:
    try:
        return float((v or "0").replace(",", "").strip() or 0)
    except ValueError:
        return 0.0


@register
class Manual(Source):
    name = "manual"
    currency = "CNY"      # 기본값 (줄마다 currency·origin 으로 바꿀 수 있음)
    origin = "CN"
    supports_newest = False   # 신상 발견에는 쓰지 않음
    explicit_only = True      # 검색하는 쇼핑몰이 아님 → sources 에 manual 을 적은 상품만

    def path(self) -> Path:
        return BASE / self.conf.get("file", "catalog/manual_prices.csv")

    def ready(self) -> bool:
        return bool(self.conf.get("enabled")) and self.path().exists()

    def rows(self) -> list[dict]:
        with open(self.path(), newline="", encoding="utf-8-sig") as f:
            return [r for r in csv.DictReader(f)
                    if (r.get("id") or "").strip() and not r["id"].strip().startswith("#")]

    def search(self, q: Query) -> list[Offer]:
        stale_days = int(self.conf.get("stale_days", 7))
        out = []
        for i, r in enumerate(self.rows()):
            if r["id"].strip() != q.spec_id:
                continue
            notes = []
            checked = (r.get("checked") or "").strip()
            try:
                age = (date.today() - datetime.strptime(checked, "%Y-%m-%d").date()).days
                if age > stale_days:
                    notes.append(f"수동 가격을 {age}일 전에 확인함 → 판매처 가격·재고 다시 확인 후 checked 날짜 갱신")
            except ValueError:
                notes.append("수동 가격에 checked(확인 날짜, 예: 2026-11-13) 없음")
            out.append(Offer(
                source=self.name, key=f"mn:{q.spec_id}:{i}",
                name=(r.get("name") or "").strip() or q.keyword,
                price=_num(r.get("price")),
                currency=(r.get("currency") or self.currency).strip().upper(),
                origin=(r.get("origin") or self.origin).strip().upper(),
                shipping=_num(r.get("shipping")),
                shipping_known=bool((r.get("shipping") or "").strip()),   # 빈 칸 = 모름 (무료배송과 구분)
                in_stock=(r.get("in_stock") or "Y").strip().upper() not in ("N", "0", "FALSE"),
                shop=(r.get("shop") or "").strip(), url=(r.get("url") or "").strip(),
                notes=notes,
            ))
        return out
