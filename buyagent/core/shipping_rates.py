"""배대지 요금표(settings.toml [shipping_profiles.*]) 확인 날짜 관리

- [shipping_checked] 에 나라별 마지막 확인 날짜를 적어 둠. max_days(기본 30)가 지나면 경고
- 날짜가 비어 있으면 '확인 기록 없음(추정값)' 경고
- 화면(web.py)에서 요금 3개를 고치면 settings.toml 의 그 줄만 바꾸고 확인 날짜를 오늘로 기록
  (자동 수집 없음: 짐스캐너 등 비교 사이트는 이용약관상 영리 목적 자동 수집 금지 → 사람이 보고 입력)
"""
from __future__ import annotations

import re
import tomllib
from datetime import date
from pathlib import Path

SIZES = ("small", "medium", "large")
MAX_FEE = 500_000


def _as_date(v) -> date | None:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v).strip())
    except ValueError:
        return None


def checked_dates(cfg: dict) -> dict[str, date | None]:
    """나라 코드 → 마지막 확인 날짜 (None = 기록 없음). [shipping_checked] 에 적힌 나라만"""
    sec = cfg.get("shipping_checked", {})
    return {k: _as_date(v) for k, v in sec.items() if k != "max_days"}


def stale_messages(cfg: dict, today: date | None = None) -> list[str]:
    """확인할 때가 된 배대지 요금표 → 경고 문장 (같은 상태면 같은 문장 → 알림은 한 번만)"""
    today = today or date.today()
    max_days = int(cfg.get("shipping_checked", {}).get("max_days", 30))
    out = []
    for origin, d in checked_dates(cfg).items():
        name = cfg.get("origins", {}).get(origin, {}).get("name", origin)
        if d is None:
            out.append(f"{name} 배대지 요금 확인 기록 없음 (지금 값은 추정값) → 쓰는 배대지 요금표를 보고 화면 '배대지 요금'에서 저장")
        elif (today - d).days > max_days:
            out.append(f"{name} 배대지 요금 확인한 지 {max_days}일 넘음 (마지막 확인 {d}) → 요금표 확인 후 화면 '배대지 요금'에서 저장")
    return out


def update_rates(settings: Path, origin: str, fees: dict[str, int], today: date | None = None) -> None:
    """settings.toml 의 [shipping_profiles.<origin>] small/medium/large 값과 [shipping_checked] <origin> 날짜만 바꿈.
    주석·다른 줄은 그대로. 형식이 이상하면 ValueError (파일은 안 바꿈)"""
    today = today or date.today()
    if not re.fullmatch(r"[A-Z][A-Z_]{1,15}", origin or ""):
        raise ValueError("나라 코드가 이상합니다")
    for k in SIZES:
        v = fees.get(k)
        if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= MAX_FEE:
            raise ValueError(f"{k} 요금은 0~{MAX_FEE:,} 사이 정수(원)로 넣어 주세요")
    if not fees["small"] <= fees["medium"] <= fees["large"]:
        raise ValueError("small ≤ medium ≤ large 순서여야 합니다")

    with settings.open(encoding="utf-8", newline="") as f:      # 줄바꿈(LF/CRLF) 그대로 유지
        text = f.read()
    lines = text.splitlines(keepends=True)
    section, done, date_done = "", set(), False
    for i, ln in enumerate(lines):
        m = re.match(r"\s*\[([^\]]+)\]", ln)
        if m:
            section = m.group(1).strip()
            continue
        if section == f"shipping_profiles.{origin}":
            m = re.match(r"(\s*)(small|medium|large)(\s*=\s*)[0-9_.]+(.*)", ln, re.S)
            if m:
                lines[i] = f"{m.group(1)}{m.group(2)}{m.group(3)}{fees[m.group(2)]}{m.group(4)}"
                done.add(m.group(2))
        elif section == "shipping_checked":
            m = re.match(rf"(\s*){origin}(\s*=\s*)(\"[^\"]*\"|[0-9-]+)(.*)", ln, re.S)
            if m:
                lines[i] = f"{m.group(1)}{origin}{m.group(2)}{today.isoformat()}{m.group(4)}"
                date_done = True
    if done != set(SIZES):
        raise ValueError(f"settings.toml [shipping_profiles.{origin}] 에서 small/medium/large 줄을 못 찾음")
    if not date_done:
        raise ValueError(f"settings.toml [shipping_checked] 에 {origin} 줄이 없음")
    new = "".join(lines)
    cfg = tomllib.loads(new)                                   # 깨진 파일은 저장하지 않음
    if any(cfg["shipping_profiles"][origin][k] != fees[k] for k in SIZES):
        raise ValueError("저장 확인 실패")
    with settings.open("w", encoding="utf-8", newline="") as f:
        f.write(new)
