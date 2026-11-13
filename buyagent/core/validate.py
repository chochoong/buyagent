"""설정·카탈로그 점검 (python main.py --check)

실제 실행 중에 조용히 틀리는 것들을 미리 찾아냄:
  - 상품 ID 중복 (가격 이력이 섞임)
  - 카테고리·배송 요금 등급·국가 설정·환율 누락 (면세 한도가 기본값 $150로 조용히 들어가는 등)
  - 소스 이름 오타, 마켓 수수료·마진 기준 오류
"""
from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

from .models import Spec

ERROR, WARN = "오류", "주의"


def _source_origins(cfg: dict, registry: dict) -> dict[str, tuple[str, str]]:
    """사용 설정된 소스 → (국가, 통화)"""
    out = {}
    for name, conf in cfg.get("sources", {}).items():
        cls = registry.get(name)
        if cls and conf.get("enabled"):
            out[name] = (cls.origin, cls.currency)
    return out


def validate(cfg: dict, specs: list[Spec], registry: dict, base: Path) -> list[tuple[str, str]]:
    issues: list[tuple[str, str]] = []
    err = lambda m: issues.append((ERROR, m))   # noqa: E731
    warn = lambda m: issues.append((WARN, m))   # noqa: E731

    cats = cfg.get("categories", {})
    origins = cfg.get("origins", {})
    profiles = cfg.get("shipping_profiles", {})
    fx_cur = set(cfg.get("fx", {}).get("manual_krw", {})) | {"KRW"}

    # ---- 소스 ----
    for name, conf in cfg.get("sources", {}).items():
        if name not in registry:
            err(f"[sources.{name}] 에 해당하는 플러그인 파일(sources/{name}.py)이 없음")
        elif conf.get("enabled"):
            cls = registry[name]
            missing = [k for k in cls.required_keys if not conf.get(k)]
            if missing:
                warn(f"소스 {name}: 키 미입력({', '.join(missing)}) → settings.local.toml 에 입력")
            soft = [k for k in cls.recommended_keys if not conf.get(k)]
            if soft and not missing:
                warn(f"소스 {name}: {', '.join(soft)} 비어 있음 → 없이 시도함. 거절되면 python main.py --probe 로 확인")
            try:
                if float(conf.get("min_interval", 1.0)) < 1.0:
                    warn(f"소스 {name}: min_interval {conf['min_interval']}초 → 초당 1회 제한에 걸릴 수 있음")
            except (TypeError, ValueError):
                err(f"소스 {name}: min_interval 값이 숫자가 아님")
    src_info = _source_origins(cfg, registry)
    if not src_info:
        warn("사용 설정(enabled = true)된 소스가 하나도 없음")

    # ---- 국가별 설정: 소스가 쓰는 국가·통화가 모두 있어야 함 ----
    for name, (origin, cur) in sorted(src_info.items()):
        if origin not in origins:
            err(f"소스 {name}의 국가 {origin}: [origins.{origin}] 없음 → 면세 한도가 기본값 $150로 계산됨")
        elif "duty_free_usd" not in origins[origin]:
            err(f"[origins.{origin}] 에 duty_free_usd(면세 한도) 없음")
        if origin not in profiles:
            err(f"소스 {name}의 국가 {origin}: [shipping_profiles.{origin}] 배대지 요금표 없음")
        if cur not in fx_cur:
            err(f"소스 {name}의 통화 {cur}: [fx.manual_krw] 에 환율 없음")
    if "USD" not in fx_cur:
        err("[fx.manual_krw] 에 USD 없음 → 면세 한도(달러) 계산 불가")

    # ---- 카테고리 ----
    for key, c in cats.items():
        if key.startswith("_"):
            continue
        if "name" not in c:
            warn(f"[categories.{key}] 에 name 없음")
        if c.get("allowed"):
            prof = c.get("shipping_profile", "medium")
            for origin in {o for o, _ in src_info.values()} & set(profiles):
                if prof not in profiles[origin]:
                    err(f"[categories.{key}] 배송 등급 '{prof}' 이 [shipping_profiles.{origin}] 에 없음")
            if not 0 <= float(c.get("duty_rate", 0.08)) < 1:
                err(f"[categories.{key}] duty_rate {c.get('duty_rate')} → 0~1 사이 소수로 (8%면 0.08)")
        elif not c.get("reason") and key not in ("_exclude",):
            warn(f"[categories.{key}] 금지 카테고리인데 reason(사유) 없음")
    for i, rule in enumerate(cfg.get("block_rules", []), 1):
        if rule.get("category") not in cats:
            err(f"{i}번째 [[block_rules]] 의 category '{rule.get('category')}' 가 카테고리에 없음")
        elif cats[rule["category"]].get("allowed"):
            warn(f"[[block_rules]] '{rule['category']}' 는 판매 가능 카테고리라 차단 효과 없음")

    # ---- 마켓·가격 기준 ----
    pricing = cfg.get("pricing", {})
    markets = cfg.get("markets", {})
    if not markets:
        err("[markets.*] 판매 마켓이 하나도 없음")
    for key, m in markets.items():
        if "fee_rate" not in m or "name" not in m:
            err(f"[markets.{key}] 에 name 또는 fee_rate 없음")
            continue
        fee = m["fee_rate"] * (1.1 if m.get("fee_vat_excluded") else 1.0)
        rates = [pricing.get("min_margin_rate", 0)] + [
            c["min_margin_rate"] for c in cats.values() if "min_margin_rate" in c]
        if fee + max(rates) >= 1:
            err(f"[markets.{key}] 수수료+최소 마진율이 100% 이상 → 권장가 계산 불가"
                + (" (fee_rate는 소수로: 10.9%면 0.109)" if m["fee_rate"] >= 1 else ""))
        elif m["fee_rate"] >= 0.5:
            warn(f"[markets.{key}] fee_rate {m['fee_rate']} → 퍼센트가 아니라 소수로 (10.9%면 0.109)")
    if int(pricing.get("price_ending", 0)) >= int(pricing.get("round_to", 1000)):
        err("[pricing] price_ending 이 round_to 보다 크거나 같음")

    # products.csv 에 my_price_<마켓> 열이 있는지
    pfile = base / "catalog" / "products.csv"
    if pfile.exists():
        with open(pfile, newline="", encoding="utf-8-sig") as f:
            header = next(csv.reader(f), [])
        for key in markets:
            if f"my_price_{key}" not in header:
                warn(f"products.csv 에 my_price_{key} 열 없음 → {markets[key].get('name', key)} 현재가 마진 점검 안 됨")

    # ---- 상품 목록 ----
    dup = [i for i, n in Counter(s.id for s in specs).items() if n > 1]
    for i in dup:
        what = "discover.csv 의 name" if i.startswith("discover:") else "products.csv 의 id"
        err(f"{what} 중복: '{i.removeprefix('discover:')}' → 가격 이력·신상 기록이 섞임")
    for s in specs:
        label = f"{'신상' if s.discover else '상품'} {s.id.removeprefix('discover:')}"
        if not s.keyword and not s.jan:
            err(f"{label}: keyword(검색어)와 jan 이 모두 비어 있음")
        for src in s.sources:
            if src not in cfg.get("sources", {}):
                err(f"{label}: sources 의 '{src}' 가 settings.toml 에 없음 (오타?)")
            elif src not in src_info:
                warn(f"{label}: sources 의 '{src}' 가 꺼져 있음(enabled = false)")
        if s.jan and not (s.jan.isdigit() and len(s.jan) in (8, 12, 13, 14)):
            warn(f"{label}: jan '{s.jan}' 형식 이상 (숫자 8·12·13·14자리)")
        if s.shipping_profile:
            used = {src_info[x][0] for x in (s.sources or src_info) if x in src_info}
            for origin in used & set(profiles):
                if s.shipping_profile not in profiles[origin]:
                    err(f"{label}: shipping_profile '{s.shipping_profile}' 이 "
                        f"[shipping_profiles.{origin}] 에 없음")
        if s.bundle_size < 0:
            err(f"{label}: bundle_size 가 음수")
        if (not s.discover and not s.must_include and not s.jan and s.sources != ["manual"]
                and cats.get(s.category, {}).get("allowed")):
            warn(f"{label}: must_include 와 jan 이 모두 비어 있음 → 엉뚱한 상품이 최저가로 잡힐 수 있음")

    # ---- 신상 발견: 최신순 정렬을 지원하는 소스가 있어야 함 ----
    newest_ok = {n for n in src_info if registry[n].supports_newest}
    for s in (x for x in specs if x.discover):
        label = f"신상 {s.name}"
        bad = [x for x in s.sources if x in registry and not registry[x].supports_newest]
        if bad:
            warn(f"{label}: {', '.join(bad)} 는 최신순 정렬이 없어 신상 발견에서 제외됨")
        if not ((set(s.sources) if s.sources else set(src_info)) & newest_ok):
            warn(f"{label}: 최신순을 지원하는 소스(예: rakuten)가 없어 신상 검색이 안 됨")

    # ---- 단어장 ----
    for w in cfg.get("glossary_unknown", []):
        warn(f"단어장(glossary.toml)에 없는 한글 '{w}' → 일본어 상품명에 걸리지 않음")
    return issues
