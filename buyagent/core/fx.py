"""환율: 통화 1단위당 원화(KRW). 자동 조회 실패 시 settings.toml 값 사용"""
from __future__ import annotations

import sys

from .http import get_json


class FX:
    def __init__(self, cfg: dict, demo: bool):
        fx = cfg["fx"]
        self.krw: dict[str, float] = {"KRW": 1.0, **fx["manual_krw"]}
        self.source = "설정값"
        if demo or not fx.get("auto", True):
            return
        try:
            cur = sorted(set(self.krw) - {"KRW"})
            d = get_json("https://api.frankfurter.dev/v1/latest",
                         {"base": "KRW", "symbols": ",".join(cur)})
            for c, rate in d["rates"].items():      # rate = 1원당 외화 → 역수
                self.krw[c] = 1 / float(rate)
            self.source = f"ECB {d.get('date')}"
        except Exception as e:
            print(f"[경고] 환율 자동 조회 실패 → 설정값 사용 ({e})", file=sys.stderr)
            self.source = "설정값(조회 실패)"

    def to_krw(self, amount: float, currency: str) -> float:
        if currency not in self.krw:
            raise KeyError(f"환율 없음: {currency} → settings.toml [fx.manual_krw]에 추가하세요")
        return amount * self.krw[currency]

    def to_usd(self, amount: float, currency: str) -> float:
        return self.to_krw(amount, currency) / self.krw["USD"]

    def summary(self) -> str:
        parts = [f"1{c}={v:,.2f}원" for c, v in self.krw.items() if c != "KRW"]
        return " / ".join(parts) + f"  ({self.source})"
