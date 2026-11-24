"""원가 · 관부가세 · 마켓별 권장 판매가 계산

개당 원가 = 상품가×환율×(1+카드수수료)
          + (배대지 요금 + 국내배송비) ÷ 묶음수량
          + 포장비
          + [면세 한도 초과 시] 예상 관부가세 ÷ 묶음수량
권장가    = 마진 ≥ 최소마진(원) 이고 마진율 ≥ 최소마진율인 가장 낮은 가격
"""
from __future__ import annotations

import math

from .fx import FX
from .models import Offer, Quote, Result


def round_price(p: float, round_to: int, ending: int) -> int:
    if ending:
        return int(math.ceil((p - ending) / round_to) * round_to + ending)
    return int(math.ceil(p / round_to) * round_to)


def effective_fee(m: dict) -> float:
    return m["fee_rate"] * (1.1 if m.get("fee_vat_excluded") else 1.0)


def margin_at(price: int, cost: int, fee: float) -> int:
    return int(round(price * (1 - fee) - cost))


def forwarding_fee(cfg: dict, origin: str, profile: str) -> float:
    table = cfg["shipping_profiles"].get(origin)
    if not table:
        raise KeyError(f"settings.toml에 [shipping_profiles.{origin}] 요금표가 없음")
    if profile not in table:
        raise KeyError(f"[shipping_profiles.{origin}]에 '{profile}' 요금이 없음")
    return float(table[profile])


def small_hint(cfg: dict, res: Result, offer: Offer, profile: str, fwd: float, bundle: int) -> None:
    """상품명·설명에 우편함 배송(メール便 등) 표시가 있는데 small 보다 큰 요금으로 계산했으면 알려 줌 (값은 안 바꿈)"""
    words = cfg.get("shipping_hints", {}).get(offer.origin, {}).get("small_keywords", [])
    small = cfg["shipping_profiles"].get(offer.origin, {}).get("small")
    if not words or small is None or profile == "small" or fwd <= small:
        return
    text = f"{offer.name} {offer.text}"
    hit = next((w for w in words if w in text), "")
    if hit:
        cost = int(round(res.cost_krw - (fwd - small) / bundle))
        res.alerts.append(f"'{hit}' 표시 → 소형 배송일 수 있음. small 이면 원가 약 {cost:,}원 "
                          f"(products.csv shipping_profile 칸에 small)")


def overseas_hint(cfg: dict, offer: Offer) -> str:
    """판매자 해외배송 가능 근거. 라쿠텐 해외배송 검색으로 온 것(api)이 아니면 상품명·설명의 표시 단어로 (대소문자 무시)"""
    if offer.overseas:
        return offer.overseas
    words = cfg.get("shipping_hints", {}).get(offer.origin, {}).get("overseas_keywords", [])
    text = f"{offer.name} {offer.text}".lower()
    hit = next((w for w in words if w.lower() in text), "")
    offer.overseas = f"표시:{hit}" if hit else ""
    return offer.overseas


def overseas_label(src: str) -> str:
    return "라쿠텐 해외배송 검색" if src == "api" else f"'{src.removeprefix('표시:')}' 표시"


def price_offer(cfg: dict, fx: FX, res: Result, offer: Offer, bundle_default: int = 0) -> None:
    c, cu = cfg["costs"], cfg["customs"]
    spec = res.spec
    cat = cfg["categories"][spec.category]
    pr = {**cfg["pricing"], **{k: v for k, v in cat.items()
                               if k in ("min_margin_krw", "min_margin_rate", "max_price_multiple")}}
    origin = cfg["origins"].get(offer.origin, {})
    res.offer = offer
    res.notice = cat.get("notice", "")

    bundle = max(1, spec.bundle_size or bundle_default or int(cat.get("bundle_size", 1)))
    res.bundle_size = bundle
    profile = spec.shipping_profile or cat.get("shipping_profile", "medium")
    fwd = forwarding_fee(cfg, offer.origin, profile)
    domestic = c["domestic_shipping_krw"]

    # 판매자 해외배송 가능이면: 판매자 → 고객 직배송(추정 요금, 일본 내 배송비·국내 택배 없음)과 배대지 경유 중 싼 쪽
    src = overseas_hint(cfg, offer)
    direct_tbl = cfg.get("shipping_direct", {}).get(offer.origin, {})
    direct = src and profile in direct_tbl
    if direct:
        dfee = float(direct_tbl[profile])
        via_fwd = fx.to_krw(offer.total, offer.currency) + fwd + domestic
        via_direct = fx.to_krw(offer.price, offer.currency) + dfee
        direct = via_direct < via_fwd
        if direct:
            res.alerts.append(f"판매자 해외배송 가능({overseas_label(src)}) → 배대지 대신 판매자 직배송 추정 "
                              f"{dfee:,.0f}원으로 계산 (배대지 경유보다 약 {via_fwd - via_direct:,.0f}원 쌈). "
                              f"판매자 페이지에서 한국 배송비·가능 여부 확인")
        else:
            res.alerts.append(f"판매자 해외배송 가능({overseas_label(src)}) — 직배송 추정 {dfee:,.0f}원이 "
                              f"배대지 경유보다 비싸 배대지 기준으로 계산")
    elif src:
        res.alerts.append(f"판매자 해외배송 가능({overseas_label(src)}) — 직배송 요금표 없음 → 배대지 기준")
    res.ship_route = "direct" if direct else "forwarder"
    ship_krw = dfee if direct else fwd + domestic          # 개당 원가에 들어가는 배송비 (묶음 1회분)
    goods_local = offer.price if direct else offer.total   # 직배송이면 일본 내 배송비는 안 듦

    unit_krw = fx.to_krw(goods_local, offer.currency)
    unit_usd = fx.to_usd(goods_local, offer.currency)
    res.goods_usd = round(unit_usd, 2)

    # 면세 한도는 '한 번에 들어오는 물품 전체' 기준 → 묶음이면 합산
    limit = origin.get("duty_free_usd", 150)
    shipment_usd = unit_usd * bundle
    res.over_duty_free = shipment_usd > limit
    if res.over_duty_free and cu.get("include_duty_if_over", True):
        taxable = unit_krw * bundle + (dfee if direct else fwd)
        duty = taxable * cat.get("duty_rate", 0.08)
        res.est_duty_krw = int(round((duty + (taxable + duty) * cu["vat_rate"]) / bundle))
        res.alerts.append(f"1회 배송 물품가 ${shipment_usd:,.2f} > ${limit} → 관부가세 발생 "
                          f"(개당 약 {res.est_duty_krw:,}원, 판매가에 포함)")

    res.shipping_profile = profile
    res.forwarding_krw = int(round(ship_krw / bundle))
    res.cost_krw = int(round(
        unit_krw * (1 + c["card_fx_fee_rate"])
        + ship_krw / bundle
        + c["packaging_krw"] + res.est_duty_krw))

    for key, m in cfg["markets"].items():
        fee = effective_fee(m)
        p1 = (res.cost_krw + pr["min_margin_krw"]) / (1 - fee)
        denom = 1 - fee - pr["min_margin_rate"]
        p2 = res.cost_krw / denom if denom > 0 else float("inf")
        rec = round_price(max(p1, p2), pr["round_to"], pr["price_ending"])
        q = Quote(m["name"], rec, margin_at(rec, res.cost_krw, fee), key=key)
        mp = spec.my_prices.get(key, 0)
        if mp > 0:
            q.my_price, q.my_margin = mp, margin_at(mp, res.cost_krw, fee)
            q.my_ok = q.my_margin >= pr["min_margin_krw"] and q.my_margin >= mp * pr["min_margin_rate"]
            if not q.my_ok:
                res.alerts.append(f"{m['name']} 현재가 {mp:,}원 → 마진 {q.my_margin:,}원 (기준 미달). "
                                  f"권장가 {rec:,}원")
        res.quotes.append(q)

    if res.ship_route != "direct":                         # 직배송이면 배대지 소형 요금 얘기는 안 맞음
        small_hint(cfg, res, offer, profile, fwd, bundle)

    mult_limit = pr.get("max_price_multiple")
    if mult_limit and res.quotes and offer.price > 0:
        mult = min(q.recommended_price for q in res.quotes) / fx.to_krw(offer.price, offer.currency)
        if mult > mult_limit:
            hint = " (묶음 배송·세트 구성 검토)" if bundle == 1 else ""
            res.alerts.append(f"권장가가 현지가의 {mult:.1f}배 → 가격 경쟁력 낮음{hint}")
    if not offer.shipping_known:
        if offer.source == "manual":    # 수동 가격의 빈 shipping 칸 → 사람이 확인해서 채워야 함
            res.alerts.append(f"현지 배송비 미확인 → 확인 후 shipping 입력 "
                              f"(지금은 {offer.shipping:,.0f} {offer.currency}로 가정)")
        else:
            res.alerts.append(f"현지 배송비 미확인 → {offer.shipping:,.0f} {offer.currency}로 가정")
