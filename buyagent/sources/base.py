"""소스(쇼핑몰) 플러그인 기본 클래스

새 쇼핑몰을 붙이려면 sources/ 폴더에 파일 하나를 만들고:

    from .base import Source, register

    @register
    class MyShop(Source):
        name = "myshop"          # settings.toml의 [sources.myshop] 과 이름을 맞춤
        currency = "USD"
        origin = "US"

        def search(self, q):     # Query → list[Offer]
            ...

settings.toml 에 [sources.myshop] enabled = true 를 추가하면 끝.
자세한 예시는 sources/_template.py 참고.
"""
from __future__ import annotations

from core.http import get_json
from core.models import Offer, Query

REGISTRY: dict[str, type["Source"]] = {}


def register(cls: type["Source"]) -> type["Source"]:
    REGISTRY[cls.name] = cls
    return cls


class Source:
    name: str = ""
    currency: str = "JPY"
    origin: str = "JP"
    required_keys: tuple[str, ...] = ()      # settings에 반드시 있어야 하는 키

    def __init__(self, conf: dict, cfg: dict):
        self.conf = conf          # [sources.<name>] 섹션
        self.cfg = cfg            # 전체 설정

    def ready(self) -> bool:
        return bool(self.conf.get("enabled")) and all(self.conf.get(k) for k in self.required_keys)

    def default_shipping(self) -> float:
        """현지 배송비를 모를 때 가정값 (현지 통화)"""
        return float(self.conf.get("default_shipping", 0))

    def get(self, url: str, params: dict | None = None, headers: dict | None = None) -> dict:
        """API 호출. 속도 제한(min_interval, 기본 1초)과 재시도(retries, 기본 2회)를 자동 적용"""
        return get_json(url, params, headers,
                        min_interval=float(self.conf.get("min_interval", 1.0)),
                        retries=int(self.conf.get("retries", 2)))

    def search(self, q: Query) -> list[Offer]:
        raise NotImplementedError
