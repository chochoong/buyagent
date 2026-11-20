"""네이버 쇼핑 최저가 — 사람이 보고 적는 값 (자동 수집 없음)

- 네이버 쇼핑 검색 API(가격)는 2026-07-31 종료, NAVER API HUB 에도 쇼핑 검색 없음 (2026-11-20 확인)
- 네이버 쇼핑 화면 자동 수집(크롤링)은 하지 않음 → 화면의 [네이버 최저가 보기] 링크로 사람이 확인하고 적음
- 저장: catalog/naver_prices.csv (id, price, checked, memo). id = products.csv 의 상품 ID
- 확인한 지 max_days(settings.toml [naver_low], 기본 30일)가 지나면 '다시 확인 필요'
"""
from __future__ import annotations

import csv
import io
import os
import re
import tempfile
from datetime import date
from pathlib import Path
from urllib.parse import quote

HEADER = ["id", "price", "checked", "memo"]
SEARCH_URL = "https://search.shopping.naver.com/search/all?query="
MAX_PRICE = 100_000_000
MAX_MEMO = 200


def path(base: Path) -> Path:
    return base / "catalog" / "naver_prices.csv"


def search_url(keyword: str) -> str:
    """네이버 쇼핑 검색 링크 (사람이 눌러서 봄). 검색어는 URL 인코딩"""
    return SEARCH_URL + quote(" ".join(str(keyword or "").split()), safe="")


def link_keyword(product: dict) -> str:
    """네이버에서 찾을 검색어: 모델명(필수 단어 첫 번째) → 없으면 한국어 상품명 → 검색어"""
    must = [w.strip() for w in (product.get("must_include") or "").split("|") if w.strip()]
    if must:
        return must[0]
    name = re.sub(r"\s*[(（\[].*$", "", product.get("name") or "").strip()    # 괄호 설명은 빼고 짧게
    return name or (product.get("keyword") or "").strip()


def load(base: Path) -> dict[str, dict]:
    p = path(base)
    if not p.exists():
        return {}
    with p.open(encoding="utf-8-sig", newline="") as f:
        return {(r.get("id") or "").strip(): r for r in csv.DictReader(f) if (r.get("id") or "").strip()}


def max_days(cfg: dict) -> int:
    return int(cfg.get("naver_low", {}).get("max_days", 30))


def status(row: dict | None, limit: int = 30, today: date | None = None) -> dict | None:
    """{price, checked, memo, days, stale} 또는 None(적은 값 없음)"""
    if not row:
        return None
    try:
        price = int(str(row.get("price") or "").replace(",", ""))
        checked = date.fromisoformat(str(row.get("checked") or "").strip())
    except ValueError:
        return None
    days = ((today or date.today()) - checked).days
    return {"price": price, "checked": checked.isoformat(), "memo": row.get("memo") or "",
            "days": days, "stale": days > limit}


def save(base: Path, pid: str, price, memo: str = "", today: date | None = None) -> dict | None:
    """값 저장 (확인 날짜 = 오늘). price 가 빈 값이면 그 상품 줄을 지움. 이상한 값은 ValueError"""
    pid = (pid or "").strip()
    if not pid:
        raise ValueError("상품 ID 가 없습니다")
    memo = " ".join(str(memo or "").split())[:MAX_MEMO]
    rows = load(base)
    if price in (None, ""):
        rows.pop(pid, None)
        new = None
    else:
        try:
            v = int(str(price).replace(",", "").replace("원", "").strip())
        except ValueError:
            raise ValueError("최저가는 숫자(원)로 넣어 주세요") from None
        if not 0 < v <= MAX_PRICE:
            raise ValueError("최저가는 1원 이상으로 넣어 주세요")
        new = {"id": pid, "price": str(v), "checked": (today or date.today()).isoformat(), "memo": memo}
        rows[pid] = new
    p = path(base)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=HEADER, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    w.writerows(rows.values())
    fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as f:
        f.write(buf.getvalue())
    os.replace(tmp, p)
    return new
