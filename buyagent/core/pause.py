"""감시 전체 멈춤 스위치 (data/watch_paused.json, git 제외)

멈춤 상태면 정기 실행(main.py)이 상품 조회·알림을 하지 않고 바로 끝남.
상품별 켜짐/꺼짐(products.csv active)은 그대로 두므로 다시 시작하면 원래대로 돌아감.
직접 검색(--search)·--demo 는 막지 않음.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def pause_file(base: Path) -> Path:
    return base / "data" / "watch_paused.json"


def paused_since(base: Path) -> str | None:
    """멈춤 상태면 멈춘 시각 문자열, 아니면 None"""
    try:
        d = json.loads(pause_file(base).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return str(d.get("since") or "?") if isinstance(d, dict) else "?"


def set_paused(base: Path, paused: bool) -> None:
    p = pause_file(base)
    if paused:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"since": f"{datetime.now():%Y-%m-%d %H:%M}"}), encoding="utf-8")
    else:
        p.unlink(missing_ok=True)
