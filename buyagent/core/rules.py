"""판매 가능 여부 규칙: 카테고리 허용, 차단 키워드, 경고 키워드, 매칭"""
from __future__ import annotations

import unicodedata

from .models import Offer, Spec


def norm(s: str) -> str:
    """전각/반각·대소문자 차이 제거 (ＳＢＴＱ０４１ == sbtq041)"""
    return unicodedata.normalize("NFKC", s or "").lower()


def category_label(cfg: dict, cat: str) -> str:
    if cat == "_exclude":
        return "중고·비정품 의심"
    return cfg["categories"].get(cat, {}).get("name", cat)


def is_allowed(cfg: dict, cat: str) -> bool:
    return bool(cfg["categories"].get(cat, {}).get("allowed", False))


def _strip_except(n: str, words: list[str]) -> str:
    """예외 단어를 지운 텍스트. 예: 'クリップ'을 지워야 'リップ'(화장품)에 걸리지 않음"""
    for w in sorted((norm(x) for x in words), key=len, reverse=True):
        n = n.replace(w, " ")
    return n


def blocked_category(cfg: dict, text: str, category: str = "") -> str | None:
    """텍스트가 '허용 안 된 카테고리'의 차단 키워드에 걸리면 그 카테고리 반환
    규칙의 except 목록에 있는 단어는 먼저 지우고 검사함 (오탐 방지)
    category 를 주면 그 카테고리의 ignore_blocks 에 있는 규칙은 건너뜀
    (예: 키보드는 'Bluetooth'가 들어가도 전자제품 차단에 안 걸리게)"""
    n = norm(text)
    ignore = set(cfg["categories"].get(category, {}).get("ignore_blocks", []))
    for rule in cfg["block_rules"]:
        if is_allowed(cfg, rule["category"]) or rule["category"] in ignore:
            continue
        t = _strip_except(n, rule.get("except", []))
        if any(norm(k) in t for k in rule["keywords"]):
            return rule["category"]
    return None


def warnings_for(cfg: dict, text: str) -> list[str]:
    n = norm(text)
    return [w["message"] for w in cfg["warn_rules"] if any(norm(k) in n for k in w["keywords"])]


def check_spec(cfg: dict, spec: Spec) -> str:
    """감시 대상 자체가 금지인지 확인. 금지면 사유 문자열, 아니면 빈 문자열"""
    cat = cfg["categories"].get(spec.category)
    if cat is None:
        return f"카테고리 '{spec.category}' 가 categories.toml에 없음"
    if not cat.get("allowed", False):
        return f"등록 금지 카테고리({cat['name']}): {cat.get('reason', '')}"
    bc = blocked_category(cfg, f"{spec.keyword} {spec.name}", spec.category)
    if bc:
        return f"검색어/상품명이 차단 규칙({category_label(cfg, bc)})에 걸림"
    return ""


def filter_offers(cfg: dict, offers: list[Offer], spec: Spec,
                  stats: dict[str, int]) -> list[Offer]:
    must = [norm(w) for w in spec.must_include]
    excl = [norm(w) for w in spec.exclude]
    trusted = [norm(s) for s in spec.trusted_shops]
    keep = []
    for o in offers:
        n = norm(o.name)
        reason = ""
        bc = blocked_category(cfg, o.name, spec.category)
        if o.used:
            reason = "중고"
        elif bc:
            reason = f"차단:{category_label(cfg, bc)}"
        elif any(w in n for w in excl):
            reason = "제외 단어"
        elif must and not all(w in n for w in must):
            reason = "상품명 불일치"
        elif spec.jan and spec.jan not in (o.jan + o.name + o.text):
            reason = "JAN 불일치"
        elif trusted and not any(t in norm(o.shop) for t in trusted):
            reason = "비신뢰 판매처"
        if reason:
            stats[reason] = stats.get(reason, 0) + 1
        else:
            keep.append(o)
    return keep
