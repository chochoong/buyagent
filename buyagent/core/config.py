"""설정 파일·카탈로그 로딩

config/settings.toml       : 환율, 비용, 마켓 수수료, 배송 요금표, 원산지별 면세 한도
config/settings.local.toml : API 키·웹훅 (git 제외). settings.toml 의 같은 항목을 덮어씀
config/glossary.toml       : 한글 → 일본어 단어장
config/categories.toml  : 카테고리 규칙 + 차단/경고 키워드
catalog/products.csv    : 고정 감시 상품 (엑셀에서 편집)
catalog/discover.csv    : 신상품 발견용 검색어
"""
from __future__ import annotations

import csv
import re
import tomllib
from pathlib import Path

from .models import Spec

LIST_SEP = "|"   # CSV 안에서 여러 값을 구분하는 기호: ちいかわ|ハチワレ
HANGUL = re.compile(r"[가-힣ㄱ-ㅎㅏ-ㅣ]")


def load_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def deep_merge(base: dict, over: dict) -> dict:
    """over 의 값으로 base 를 덮어씀 (하위 섹션까지)"""
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _gkey(word: str) -> str:
    """단어장 조회용 키: 띄어쓰기 무시 ("특전 포함" == "특전포함")"""
    return re.sub(r"\s+", "", word)


def load_glossary(base: Path) -> dict[str, list[str]]:
    """config/glossary.toml → {"특전포함": ["特典付き", ...]}"""
    path = base / "config" / "glossary.toml"
    if not path.exists():
        return {}
    words = load_toml(path).get("words", {})
    return {_gkey(k): ([v] if isinstance(v, str) else list(v)) for k, v in words.items()}


def expand_words(words: list[str], glossary: dict[str, list[str]],
                 used: dict[str, list[str]], unknown: set[str]) -> list[str]:
    """한글 단어에 단어장의 일본어 표현을 더함 (한글 원래 단어도 남김 → 직접 입력한 한글 상품명에도 걸림).
    단어장에 없는 한글은 그대로 두고 unknown에 기록"""
    out: list[str] = []
    for w in words:
        k = _gkey(w)
        if k in glossary:
            out += glossary[k] + [w]
            used[w] = glossary[k]
        else:
            if HANGUL.search(w):
                unknown.add(w)
            out.append(w)
    return list(dict.fromkeys(out))      # 순서 유지하며 중복 제거


def load_config(base: Path, settings_path: Path | None = None) -> dict:
    settings_path = settings_path or base / "config" / "settings.toml"
    cfg = load_toml(settings_path)
    local = settings_path.with_name("settings.local.toml")
    cfg["_local_settings"] = str(local) if local.exists() else ""
    if local.exists():
        deep_merge(cfg, load_toml(local))
    cats = load_toml(base / "config" / "categories.toml")
    cfg["categories"] = cats.get("categories", {})
    cfg["block_rules"] = cats.get("block_rules", [])
    cfg["warn_rules"] = cats.get("warn_rules", [])

    # 규칙의 keywords / except 에 쓴 한글 → 일본어
    glossary = load_glossary(base)
    used: dict[str, list[str]] = {}
    unknown: set[str] = set()
    for rule in cfg["block_rules"]:
        rule["keywords"] = expand_words(rule.get("keywords", []), glossary, used, unknown)
        rule["except"] = expand_words(rule.get("except", []), glossary, used, unknown)
    for rule in cfg["warn_rules"]:
        rule["keywords"] = expand_words(rule.get("keywords", []), glossary, used, unknown)
    cfg["glossary_used"] = used
    cfg["glossary_unknown"] = sorted(unknown)
    return cfg


def _split(v: str | None) -> list[str]:
    return [x.strip() for x in (v or "").split(LIST_SEP) if x.strip()]


def _int(v: str | None) -> int:
    try:
        return int(float((v or "0").replace(",", "")))
    except ValueError:
        return 0


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    # utf-8-sig: 엑셀에서 'CSV UTF-8'로 저장한 파일도 읽힘
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    return [r for r in rows
            if (r.get("active") or "Y").strip().upper() not in ("N", "0", "FALSE")
            and (r.get("id") or r.get("name") or "").strip()
            and not (r.get("id") or r.get("name") or "").strip().startswith("#")]


def load_specs(base: Path, markets: dict) -> list[Spec]:
    specs = []
    for r in _rows(base / "catalog" / "products.csv"):
        specs.append(Spec(
            id=r["id"].strip(), name=(r.get("name") or r["id"]).strip(),
            category=(r.get("category") or "").strip(), keyword=(r.get("keyword") or "").strip(),
            jan=(r.get("jan") or "").strip(),
            must_include=_split(r.get("must_include")), exclude=_split(r.get("exclude")),
            trusted_shops=_split(r.get("trusted_shops")), sources=_split(r.get("sources")),
            shipping_profile=(r.get("shipping_profile") or "").strip(),
            bundle_size=_int(r.get("bundle_size")),
            my_prices={k: _int(r.get(f"my_price_{k}")) for k in markets},
        ))
    for r in _rows(base / "catalog" / "discover.csv"):
        name = r["name"].strip()
        specs.append(Spec(
            id=f"discover:{name}", name=name, category=(r.get("category") or "").strip(),
            keyword=(r.get("keyword") or "").strip(),
            must_include=_split(r.get("must_include")), exclude=_split(r.get("exclude")),
            sources=_split(r.get("sources")), discover=True,
        ))
    return specs
