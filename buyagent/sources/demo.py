"""데모 소스: API 키 없이 동작 확인용 예시 데이터 (--demo 옵션에서만 사용)

검색어의 첫 단어가 상품명에 들어 있는 상품을 돌려줍니다.
나머지 걸러내기(차단·필수 단어·JAN 등)는 실제와 똑같이 공통 코드가 처리합니다.
"""
from __future__ import annotations

from core.models import Offer, Query
from core.rules import norm

from .base import Source, register


def _o(key, name, price, *, cur="JPY", origin="JP", free=True, stock=True, shop="ショップ",
       jan="", used=False, src="demo"):
    return Offer(source=src, key=key, name=name, price=price, currency=cur, origin=origin,
                 shipping=0 if free else (800 if cur == "JPY" else 8), shipping_known=free,
                 in_stock=stock, shop=shop, url=f"https://example.com/{key}", jan=jan, used=used)


DATA = [
    # --- 시계 ---
    _o("w1", "[セイコー]SEIKO SELECTION SBTQ041 クロノグラフ", 15980, shop="時計ショップA"),
    _o("w2", "セイコー SBTQ041 腕時計 メンズ", 14900, free=False, shop="ウォッチB"),
    _o("w3", "セイコー SBTQ041 純正ベルト 交換用", 4800, shop="パーツC"),
    _o("w4", "セイコー SBTQ041 美品", 9800, shop="リユースD", used=True),
    # --- 치이카와 ---
    _o("a1", "ちいかわ アクリルスタンド ハチワレ 公式", 1320, free=False, shop="キャラクターショップE"),
    _o("a2", "ちいかわ アクリルスタンド ハチワレ", 1650, shop="グッズF"),
    _o("a3", "ちいかわ アクリルスタンド うさぎ", 1320, shop="グッズF"),
    _o("a4", "ちいかわ ハチワレ ぬいぐるみ アクリルスタンド付き", 3300, shop="G"),
    _o("n1", "ちいかわ ふわふわアクリルマグネット ちいかわ", 990, shop="グッズF"),
    _o("n2", "ちいかわ アクリルキーホルダー うさぎ", 880, free=False, shop="グッズF"),
    _o("n3", "ちいかわ アクリル クッキー缶", 1980, shop="H"),
    _o("n4", "ちいかわ ハッピーセット アクリル おもちゃ", 2500, shop="I"),
    _o("k1", "ちいかわ フィギュアキーホルダー ハチワレ", 1100, shop="グッズF"),
    _o("k2", "ちいかわ マスコットキーホルダー ちいかわ", 1430, shop="J"),
    _o("k3", "ちいかわ ラバーキーホルダー", 770, stock=False, shop="K"),
    # --- 새로 추가한 일반 상품 예시 ---
    _o("h1", "ほぼ日手帳 2027 オリジナル A6 本体", 2750, shop="文具L", jan="4580000000011"),
    _o("h2", "ほぼ日手帳 2027 オリジナル A6 カバー付き", 4950, shop="文具L", jan="4580000000028"),
    _o("c1", "キャンメイク マシュマロフィニッシュパウダー", 1034, shop="コスメM"),
    _o("s1", "Stanley Quencher H2.0 Tumbler 40oz", 45.0, cur="USD", origin="US",
       shop="US Store N", jan="0041604000001"),
]


@register
class Demo(Source):
    name = "demo"

    def search(self, q: Query) -> list[Offer]:
        if q.jan:
            return [o for o in DATA if o.jan == q.jan]
        head = norm(q.keyword.split()[0]) if q.keyword.split() else ""
        return [o for o in DATA if head and head in norm(o.name)]
