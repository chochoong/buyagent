"""출력: 콘솔 · CSV · 웹훅"""
from __future__ import annotations

import csv
import sys
import time
from collections.abc import Iterator
from pathlib import Path

from .http import post_json
from .models import Result


def fmt_result(r: Result, indent: str = "  ") -> list[str]:
    if r.blocked_reason:
        return [f"{indent}⛔ {r.blocked_reason}"]
    lines = []
    if r.offer:
        o = r.offer
        ship = f"{o.shipping:,.0f}" + ("" if o.shipping_known else "(가정)")
        lines.append(f"{indent}최저가: {o.price:,.0f} + 배송 {ship} = {o.total:,.0f} {o.currency} "
                     f"[{o.source} · {o.shop}]")
        b = f", {r.bundle_size}개 묶음 기준" if r.bundle_size > 1 else ""
        lines.append(f"{indent}개당 원가: {r.cost_krw:,}원 (개당 ${r.goods_usd}{b})")
        for q in r.quotes:
            s = f"{indent}[{q.market}] 권장가 {q.recommended_price:,}원 → 마진 {q.margin:,}원"
            if q.my_price:
                s += f" | 현재가 {q.my_price:,}원 → 마진 {q.my_margin:,}원 {'✓' if q.my_ok else '✗'}"
            lines.append(s)
    if r.excluded:
        lines.append(f"{indent}제외됨: " + ", ".join(f"{k} {v}건" for k, v in r.excluded.items()))
    lines += [f"{indent}🔔 {a}" for a in r.events + r.alerts]
    return lines


def _status(r: Result) -> str:
    if r.blocked_reason:
        return "차단"
    if r.offer:
        return "판매가능"
    return "조회실패" if r.failed else "재고없음"


def write_csv(rows: list[tuple[str, Result]], markets: list[str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = ["구분", "ID", "상품명", "카테고리", "상태", "소스", "판매처", "URL", "현지가", "현지배송",
              "통화", "묶음수량", "개당원가(원)"]
    for m in markets:
        header += [f"{m}_권장가", f"{m}_마진", f"{m}_현재가", f"{m}_현재가마진"]
    header += ["상세페이지 고지문", "알림"]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        for kind, r in rows:
            o = r.offer
            name = o.name if (kind == "신상" and o) else r.spec.name
            row = [kind, r.spec.id if kind != "신상" else (o.key if o else ""), name,
                   r.spec.category, _status(r),
                   o.source if o else "", o.shop if o else "", o.url if o else "",
                   o.price if o else "", o.shipping if o else "", o.currency if o else "",
                   r.bundle_size if o else "", r.cost_krw or ""]
            qs = {q.market: q for q in r.quotes}
            for m in markets:
                q = qs.get(m)
                row += ([q.recommended_price, q.margin, q.my_price or "",
                         q.my_margin if q.my_price else ""] if q else [""] * 4)
            msgs = ([r.blocked_reason] if r.blocked_reason else []) + r.events + r.alerts
            row += [r.notice, " / ".join(msgs)]
            w.writerow(row)


def _chunks(lines: list[str], limit: int) -> Iterator[str]:
    """메시지 길이 제한에 맞게 줄 단위로 나눔 (잘라서 버리지 않음)"""
    buf = ""
    for ln in lines:
        ln = ln[:limit]
        if buf and len(buf) + 1 + len(ln) > limit:
            yield buf
            buf = ln
        else:
            buf = f"{buf}\n{ln}" if buf else ln
    if buf:
        yield buf


def send_webhook(url: str, lines: list[str]) -> None:
    if not url or not lines:
        return
    discord = "discord" in url
    limit = 1900 if discord else 3500            # Discord 2000자 제한, Slack은 여유 있게
    for i, text in enumerate(_chunks(["🛒 구매대행 모니터 알림"] + lines, limit)):
        if i:
            time.sleep(1.0)                      # 웹훅 속도 제한 회피
        try:
            post_json(url, {"content": text} if discord else {"text": text})
        except Exception as e:
            print(f"[경고] 웹훅 전송 실패: {e}", file=sys.stderr)
            return
