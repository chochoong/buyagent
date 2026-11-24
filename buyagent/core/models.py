"""공통 데이터 구조"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Query:
    """소스(쇼핑몰)에 보내는 검색 요청"""
    keyword: str
    jan: str = ""
    newest: bool = False      # True면 최신순(신상 발견용), False면 가격순
    spec_id: str = ""         # 감시 대상 ID (수동 가격 소스가 사용)


@dataclass
class Offer:
    """쇼핑몰 판매 오퍼 1건 (모든 소스가 이 형식으로 변환해서 반환)"""
    source: str               # 소스 이름 (예: rakuten)
    key: str                  # 소스 안에서 고유한 ID
    name: str
    price: float              # 현지 통화 가격
    currency: str             # JPY, USD ...
    origin: str               # 발송 국가 코드 (JP, US ...)
    shipping: float = 0.0     # 현지 내 배송비 (현지 통화)
    shipping_known: bool = True
    in_stock: bool = True
    shop: str = ""
    url: str = ""
    jan: str = ""
    text: str = ""            # 설명문 등 (JAN 확인용)
    used: bool = False
    notes: list[str] = field(default_factory=list)   # 소스가 붙이는 경고 (예: 가격 확인한 지 오래됨)
    overseas: str = ""        # 판매자 해외배송 가능 근거: "api"(라쿠텐 해외배송 검색) / "표시:<단어>"(상품명·설명) / ""(모름)

    @property
    def total(self) -> float:
        return self.price + self.shipping


@dataclass
class Spec:
    """감시 대상 1건 (products.csv / discover.csv 한 줄)"""
    id: str
    name: str
    category: str
    keyword: str
    jan: str = ""
    must_include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    trusted_shops: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)      # 비우면 활성화된 모든 소스
    shipping_profile: str = ""                             # 비우면 카테고리 기본값
    bundle_size: int = 0                                   # 0이면 카테고리/CLI 기본값
    my_prices: dict[str, int] = field(default_factory=dict)
    discover: bool = False
    hub_product_id: int | None = None                      # trade_hub 품목 번호 (대시보드 연결용)


@dataclass
class Quote:
    market: str
    recommended_price: int
    margin: int
    my_price: int | None = None
    my_margin: int | None = None
    my_ok: bool | None = None
    key: str = ""                 # settings.toml [markets.<key>] (예: coupang)


@dataclass
class Result:
    spec: Spec
    offer: Offer | None = None
    blocked_reason: str = ""
    goods_usd: float = 0.0
    over_duty_free: bool = False
    est_duty_krw: int = 0
    cost_krw: int = 0
    forwarding_krw: int = 0                           # 개당 원가 중 배대지·국제배송비 (+국내배송비) ÷ 묶음수량
    shipping_profile: str = ""                        # small / medium / large
    ship_route: str = "forwarder"                     # forwarder = 배대지 경유 / direct = 판매자 해외배송(직배송 추정 요금)
    bundle_size: int = 1
    quotes: list[Quote] = field(default_factory=list)
    events: list[str] = field(default_factory=list)   # 이번에 바뀐 것 (품절·재입고·원가 변동) → 항상 알림
    alerts: list[str] = field(default_factory=list)   # 상태 경고 → 새로 생겼을 때만 알림
    failed: bool = False                              # 조회 실패·처리 오류
    notice: str = ""
    excluded: dict[str, int] = field(default_factory=dict)
