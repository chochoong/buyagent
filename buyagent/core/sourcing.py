"""사입(수입 후 재고 판매) 원가 계산 — python sourcing.py

구매대행(1개씩, 목록통관)과 달리 사입은 여러 개를 한 번에 들여와 정식 수입신고(일반통관)를 합니다.
  · 면세 한도 없음 → 관세·부가세가 항상 붙음
  · 국제 배송비는 무게(또는 부피) 기준, 같은 batch(묶음 발송)끼리 나눠 냄
  · 통관 수수료·국내 입고 택배비는 batch 당 1번 → 상품 금액 비율로 나눔
  · 사업자 유형에 따라 부가세 처리가 다름 (settings: [tax] type)

개당 원가 = [상품가+중국 내 배송비]×환율×(1+카드수수료) + 구매대행 수수료
          + 국제 배송비 + 관세 (+ 부가세: 환급 안 되는 경우만)
          + 통관 수수료·입고비 + 인증비 ÷ 수량 + 포장비 + 고객 발송 택배비
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from .fx import FX
from .pricing import round_price
from .rules import category_label, warnings_for

SIZE_SEP = re.compile(r"\s*[x×*X]\s*")


@dataclass
class Item:
    id: str
    name: str
    category: str
    unit_price: float            # 1688 단가 (현지 통화)
    qty: int
    currency: str = "CNY"
    cn_shipping: float = 0.0     # 중국 내 배송비 (판매자 → 배대지, 이 줄 전체)
    weight_kg: float = 0.0       # 개당 포장 무게
    size_cm: tuple[float, float, float] | None = None   # 개당 포장 크기
    batch: str = ""              # 같은 값끼리 한 번에 발송 (비우면 단독 발송)
    cert_cost_krw: int = 0       # KC 인증 등 일회성 비용 (수량으로 나눔)
    market_prices: dict[str, int] = field(default_factory=dict)   # 국내 시장 경쟁가
    url: str = ""
    memo: str = ""
    naver_query: str = ""        # 네이버 시세 검색어 (비우면 name)
    naver_must: list[str] = field(default_factory=list)      # 네이버 상품명에 꼭 있어야 할 단어
    naver_exclude: list[str] = field(default_factory=list)   # 네이버 상품명에 있으면 뺄 단어
    market_note: str = ""        # 네이버 시세 요약 (--market)


@dataclass
class MarketQuote:
    market: str
    recommended_price: int
    margin: int
    market_price: int = 0
    margin_at_market: int | None = None
    ok_at_market: bool | None = None


@dataclass
class Line:
    item: Item
    blocked: str = ""
    alerts: list[str] = field(default_factory=list)
    goods_krw: int = 0           # 상품가+중국 내 배송 (카드수수료 포함)
    agent_fee_krw: int = 0
    freight_krw: int = 0         # 국제 배송 (이 줄 몫)
    fixed_krw: int = 0           # 통관 수수료·입고비 (이 줄 몫)
    duty_krw: int = 0
    vat_krw: int = 0             # 수입 부가세
    unit_cost: int = 0           # 개당 원가 (판매 비용 포함)
    cash_total: int = 0          # 이 줄에 들어가는 현금 (부가세 포함, 판매 전 시점)
    quotes: list[MarketQuote] = field(default_factory=list)


# ---------------------------------------------------------------- CSV
def _num(v: str | None, default: float = 0.0) -> float:
    try:
        s = (v or "").replace(",", "").strip()
        return float(s) if s else default
    except ValueError:
        return default


def _split(v: str | None) -> list[str]:
    return [x.strip() for x in (v or "").split("|") if x.strip()]


def parse_size(v: str | None) -> tuple[float, float, float] | None:
    parts = [p for p in SIZE_SEP.split((v or "").strip()) if p]
    if len(parts) != 3:
        return None
    try:
        a, b, c = (float(p) for p in parts)
    except ValueError:
        return None
    return (a, b, c) if min(a, b, c) > 0 else None


def load_items(path: Path, markets: dict) -> list[Item]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        iid = (r.get("id") or "").strip()
        if not iid or iid.startswith("#") or (r.get("active") or "Y").strip().upper() in ("N", "0", "FALSE"):
            continue
        out.append(Item(
            id=iid, name=(r.get("name") or iid).strip(), category=(r.get("category") or "").strip(),
            unit_price=_num(r.get("unit_price")), qty=int(_num(r.get("qty"))),
            currency=(r.get("currency") or "CNY").strip().upper(),
            cn_shipping=_num(r.get("cn_shipping")), weight_kg=_num(r.get("weight_kg")),
            size_cm=parse_size(r.get("size_cm")), batch=(r.get("batch") or "").strip(),
            cert_cost_krw=int(_num(r.get("cert_cost_krw"))),
            market_prices={k: int(_num(r.get(f"price_{k}"))) for k in markets},
            url=(r.get("url") or "").strip(), memo=(r.get("memo") or "").strip(),
            naver_query=(r.get("naver_query") or "").strip(),
            naver_must=_split(r.get("naver_must")), naver_exclude=_split(r.get("naver_exclude")),
        ))
    return out


# ---------------------------------------------------------------- 계산
def freight_basis(item: Item, mode: dict) -> float:
    """국제 배송비 (최소요금 적용 전). 무게와 부피 중 큰 쪽 기준"""
    kg = item.weight_kg * item.qty
    vol_cm3 = (item.size_cm[0] * item.size_cm[1] * item.size_cm[2] * item.qty) if item.size_cm else 0.0
    by_kg = kg * mode.get("krw_per_kg", 0)
    by_vol = 0.0
    if vol_cm3:
        if mode.get("krw_per_cbm"):                     # 해운: CBM(㎥) 단가
            by_vol = vol_cm3 / 1_000_000 * mode["krw_per_cbm"]
        if mode.get("volumetric_divisor"):              # 항공: 부피무게(kg) = 가로×세로×높이 ÷ 6000
            by_vol = max(by_vol, vol_cm3 / mode["volumetric_divisor"] * mode.get("krw_per_kg", 0))
    return max(by_kg, by_vol)


def _sale_factor(sc: dict, market: dict) -> float:
    """판매가 1원당 남는 금액(원가 빼기 전). 사업자 유형별 부가세 처리"""
    tax = sc.get("tax", {}).get("type", "general")
    fee = market["fee_rate"]
    if tax == "general":     # 일반과세자: 판매가의 1/11은 부가세로 납부, 수수료 부가세는 공제
        fee_net = fee if market.get("fee_vat_excluded") else fee / 1.1
        return 1 / 1.1 - fee_net
    fee_eff = fee * (1.1 if market.get("fee_vat_excluded") else 1.0)
    if tax == "simple":      # 간이과세자: 판매가 × 업종 부가가치율 × 10% 정도 납부
        return 1 - fee_eff - sc["tax"].get("simple_vat_rate_of_sales", 0.015)
    return 1 - fee_eff       # none: 부가세 고려 안 함


def calculate(cfg: dict, sc: dict, fx: FX, items: list[Item], method: str = "") -> list[Line]:
    method = method or sc["freight"]["method"]
    mode = sc["freight"][method]
    cu, sale, tax = sc["customs"], sc["sale"], sc.get("tax", {})
    vat_refund = tax.get("type", "general") == "general"
    card = sc["purchase"].get("card_fx_fee_rate", 0.0)
    agent_rate = sc["purchase"].get("agent_fee_rate", 0.0)
    cats = cfg["categories"]

    lines = [Line(item=it) for it in items]
    for ln in lines:
        it, cat = ln.item, cats.get(ln.item.category)
        if cat is None:
            ln.blocked = f"카테고리 '{it.category}' 가 categories.toml에 없음"
        elif not cat.get("allowed", False):
            ln.blocked = f"금지 카테고리({cat.get('name', it.category)}): {cat.get('reason', '')}"
        elif not cat.get("bulk_ok", False) and not it.cert_cost_krw:
            ln.blocked = (f"사입 불가({cat.get('name', it.category)}): "
                          + cat.get("bulk_reason", "categories.toml 에서 bulk_ok = true 가 아님")
                          + " → 인증을 받았다면 cert_cost_krw 에 인증 비용을 넣으세요")
        elif it.qty <= 0 or it.unit_price <= 0:
            ln.blocked = "unit_price(단가)와 qty(수량)를 입력하세요"
        if not ln.blocked:
            if not it.weight_kg:
                ln.alerts.append("weight_kg(개당 무게) 없음 → 국제 배송비가 0으로 계산됨")
            ln.alerts += warnings_for(cfg, it.name)
            goods = fx.to_krw(it.unit_price * it.qty + it.cn_shipping, it.currency)
            ln.goods_krw = int(round(goods * (1 + card)))
            ln.agent_fee_krw = int(round(goods * agent_rate * (1 + card)))

    # batch 별로 국제 배송비·고정비 나누기
    groups: dict[str, list[Line]] = {}
    for i, ln in enumerate(lines):
        if not ln.blocked:
            groups.setdefault(ln.item.batch or f"_single{i}", []).append(ln)
    for group in groups.values():
        bases = [freight_basis(ln.item, mode) for ln in group]
        total = max(sum(bases), float(mode.get("min_krw", 0)))
        wsum = sum(bases)
        fixed = cu.get("broker_fee_krw", 0) + cu.get("other_fees_krw", 0) + sale.get("inbound_krw", 0)
        vsum = sum(ln.goods_krw for ln in group) or 1
        for ln, b in zip(group, bases):
            share = b / wsum if wsum else 1 / len(group)
            ln.freight_krw = int(round(total * share))
            ln.fixed_krw = int(round(fixed * ln.goods_krw / vsum))
        if len(group) > 1:
            for ln in group:
                ln.alerts.append(f"묶음 발송 '{group[0].item.batch}' ({len(group)}개 상품)에서 "
                                 f"배송비 {ln.freight_krw:,}원·통관비 {ln.fixed_krw:,}원 분담")

    pr_base = cfg["pricing"]
    for ln in (x for x in lines if not x.blocked):
        it, cat = ln.item, cats[ln.item.category]
        # 과세가격(CIF) = 물품가 + 운임. 구매대행 수수료(구매수수료)는 과세가격에 넣지 않음
        cif = fx.to_krw(it.unit_price * it.qty + it.cn_shipping, it.currency) + ln.freight_krw
        duty = cif * cat.get("duty_rate", 0.08)
        ln.duty_krw = int(round(duty))
        ln.vat_krw = int(round((cif + duty) * cu.get("vat_rate", 0.10)))
        landed = (ln.goods_krw + ln.agent_fee_krw + ln.freight_krw + ln.fixed_krw + ln.duty_krw
                  + (0 if vat_refund else ln.vat_krw) + it.cert_cost_krw)
        ln.unit_cost = int(round(landed / it.qty + sale.get("packaging_krw", 0)
                                 + sale.get("outbound_shipping_krw", 0)))
        ln.cash_total = (ln.goods_krw + ln.agent_fee_krw + ln.freight_krw + ln.fixed_krw
                         + ln.duty_krw + ln.vat_krw + it.cert_cost_krw)
        if it.cert_cost_krw:
            ln.alerts.append(f"인증비 {it.cert_cost_krw:,}원 ÷ {it.qty}개 = 개당 "
                             f"{it.cert_cost_krw // it.qty:,}원 포함")

        pr = {**pr_base, **sc.get("pricing", {}),
              **{k: v for k, v in cat.items() if k in ("min_margin_krw", "min_margin_rate")}}
        for key, m in cfg["markets"].items():
            k = _sale_factor(sc, m)
            p1 = (ln.unit_cost + pr["min_margin_krw"]) / k if k > 0 else float("inf")
            p2 = ln.unit_cost / (k - pr["min_margin_rate"]) if k > pr["min_margin_rate"] else float("inf")
            rec = round_price(max(p1, p2), pr["round_to"], pr["price_ending"]) if k > 0 else 0
            q = MarketQuote(m["name"], rec, int(round(rec * k - ln.unit_cost)))
            mp = it.market_prices.get(key, 0)
            if mp > 0:
                q.market_price = mp
                q.margin_at_market = int(round(mp * k - ln.unit_cost))
                q.ok_at_market = (q.margin_at_market >= pr["min_margin_krw"]
                                  and q.margin_at_market >= mp * pr["min_margin_rate"])
                if not q.ok_at_market:
                    ln.alerts.append(f"{m['name']} 시장가 {mp:,}원에 팔면 개당 마진 {q.margin_at_market:,}원 "
                                     f"(기준 미달, 권장가 {rec:,}원)")
            ln.quotes.append(q)
    return lines


# ---------------------------------------------------------------- 출력
def fmt_line(cfg: dict, ln: Line, method: str, sale: dict | None = None) -> list[str]:
    it = ln.item
    head = f"■ {it.name}  ({category_label(cfg, it.category)}) × {it.qty}개"
    if ln.blocked:
        return [head, f"  ⛔ {ln.blocked}"] + ([f"  📊 {it.market_note}"] if it.market_note else [])
    q = it.qty
    per_sale = int((sale or {}).get("packaging_krw", 0) + (sale or {}).get("outbound_shipping_krw", 0))
    out = [head,
           f"  매입: {it.unit_price:,.2f} {it.currency} × {q}개"
           + (f" + 중국 내 배송 {it.cn_shipping:,.0f} {it.currency}" if it.cn_shipping else "")
           + f"  [{ '해운' if method == 'sea' else '항공' }]",
           f"  개당 원가: {ln.unit_cost:,}원  (상품 {ln.goods_krw // q:,} · 대행수수료 {ln.agent_fee_krw // q:,}"
           f" · 국제배송 {ln.freight_krw // q:,} · 관세 {ln.duty_krw // q:,} · 통관·입고 {ln.fixed_krw // q:,}"
           f" · 포장·발송 {per_sale:,})",
           f"  총 투입금: {ln.cash_total:,}원 (수입 부가세 {ln.vat_krw:,}원 포함)"]
    for mq in ln.quotes:
        s = f"  [{mq.market}] 권장가 {mq.recommended_price:,}원 → 개당 마진 {mq.margin:,}원"
        if mq.market_price:
            s += (f" | 시장가 {mq.market_price:,}원 → 마진 {mq.margin_at_market:,}원 "
                  f"{'✓' if mq.ok_at_market else '✗'}")
        out.append(s)
    if it.market_note:
        out.append(f"  📊 {it.market_note}")
    out += [f"  🔔 {a}" for a in ln.alerts]
    return out


def write_csv(lines: list[Line], markets: list[str], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = ["ID", "상품명", "카테고리", "상태", "수량", "단가", "통화", "개당원가(원)", "총투입금(원)",
              "상품(원)", "대행수수료(원)", "국제배송(원)", "관세(원)", "수입부가세(원)", "통관·입고(원)"]
    for m in markets:
        header += [f"{m}_권장가", f"{m}_개당마진", f"{m}_시장가", f"{m}_시장가마진"]
    header += ["네이버 시세", "URL", "알림"]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        for ln in lines:
            it = ln.item
            row = [it.id, it.name, it.category, "사입불가" if ln.blocked else "계산완료", it.qty,
                   it.unit_price, it.currency, ln.unit_cost or "", ln.cash_total or "",
                   ln.goods_krw or "", ln.agent_fee_krw or "", ln.freight_krw or "", ln.duty_krw,
                   ln.vat_krw or "", ln.fixed_krw or ""]
            qs = {q.market: q for q in ln.quotes}
            for m in markets:
                q = qs.get(m)
                row += ([q.recommended_price, q.margin, q.market_price or "",
                         q.margin_at_market if q.market_price else ""] if q else [""] * 4)
            row += [it.market_note, it.url, " / ".join(([ln.blocked] if ln.blocked else []) + ln.alerts)]
            w.writerow(row)
