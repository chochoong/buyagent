"""sources/ 폴더의 모든 플러그인을 자동으로 불러옴 (_로 시작하는 파일 제외)"""
from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path

from .base import REGISTRY, Source

for _m in pkgutil.iter_modules([str(Path(__file__).parent)]):
    if _m.name not in ("base",) and not _m.name.startswith("_"):
        importlib.import_module(f"{__name__}.{_m.name}")


def build_sources(cfg: dict, demo: bool) -> dict[str, Source]:
    if demo:
        return {"demo": REGISTRY["demo"]({"enabled": True}, cfg)}
    active = {}
    for name, conf in cfg.get("sources", {}).items():
        cls = REGISTRY.get(name)
        if cls is None:
            print(f"[경고] [sources.{name}] 에 해당하는 플러그인 파일이 없음")
            continue
        src = cls(conf, cfg)
        if src.ready():
            active[name] = src
    return active
