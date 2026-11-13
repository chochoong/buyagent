#!/usr/bin/env python3
"""
사입 원가 계산기 (중국 1688 등 → 한국 재고 판매)

  python sourcing.py              # catalog/sourcing.csv 전체 계산
  python sourcing.py --air        # 항공 배송 기준으로 계산
  python sourcing.py --only keycap
  python sourcing.py --offline    # 환율 자동 조회 안 하고 settings.toml 값 사용
  python sourcing.py --market     # 네이버 쇼핑 시세를 가져와 마진 계산 (네이버 키 필요)

설정: config/sourcing.toml (구매대행 수수료, 국제 배송 요금, 통관비, 부가세 처리)
결과: 화면 + reports/sourcing_날짜.csv
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from core.config import load_config, load_toml          # noqa: E402
from core.fx import FX                                    # noqa: E402
from core.naver import Naver                              # noqa: E402
from core.sourcing import calculate, fmt_line, load_items, write_csv  # noqa: E402


def fill_market_prices(cfg: dict, items: list) -> None:
    """네이버 쇼핑에서 국내 시세를 가져와 price_naver 가 빈 상품에 채움"""
    nv = Naver(cfg.get("naver", {}))
    if not nv.ready():
        print("[경고] 네이버 키 없음 → config/settings.local.toml 의 [naver] client_id / client_secret 을 넣으세요",
              file=sys.stderr)
        return
    how = cfg["naver"].get("market_price", "p25")
    for it in items:
        try:
            m = nv.market(it.naver_query or it.name, it.naver_must, it.naver_exclude)
        except Exception as e:
            it.market_note = f"네이버 조회 실패: {e}"
            continue
        it.market_note = m.summary()
        if "naver" in it.market_prices and not it.market_prices["naver"] and m.items:
            it.market_prices["naver"] = m.stat(how)
            it.market_note += f" → 스마트스토어 시장가 {m.stat(how):,}원으로 계산"


def main() -> None:
    ap = argparse.ArgumentParser(description="사입 원가 계산기")
    ap.add_argument("--air", action="store_true", help="항공 배송 기준")
    ap.add_argument("--sea", action="store_true", help="해운 배송 기준")
    ap.add_argument("--only", default="", help="ID에 이 문자열이 들어간 것만")
    ap.add_argument("--offline", action="store_true", help="환율 자동 조회 안 함")
    ap.add_argument("--market", action="store_true",
                    help="네이버 쇼핑 시세로 price_naver 빈 칸 채우기 (네이버 키 필요)")
    args = ap.parse_args()

    cfg = load_config(BASE)
    sc = load_toml(BASE / "config" / "sourcing.toml")
    items = [i for i in load_items(BASE / "catalog" / "sourcing.csv", cfg["markets"]) if args.only in i.id]
    if not items:
        sys.exit("catalog/sourcing.csv 에 계산할 상품이 없습니다.")
    method = "air" if args.air else "sea" if args.sea else sc["freight"]["method"]
    fx = FX(cfg, args.offline)
    print(f"\n환율: {fx.summary()}\n배송: {'항공' if method == 'air' else '해운'}"
          f" | 부가세: {sc.get('tax', {}).get('type', 'general')}\n" + "=" * 72)

    if args.market:
        fill_market_prices(cfg, items)
    lines = calculate(cfg, sc, fx, items, method)
    for ln in lines:
        print()
        print("\n".join(fmt_line(cfg, ln, method, sc["sale"])))

    ok = [ln for ln in lines if not ln.blocked]
    print("\n" + "=" * 72)
    print(f"계산 {len(ok)}건 / 사입 불가 {len(lines) - len(ok)}건 | "
          f"총 투입금 {sum(ln.cash_total for ln in ok):,}원 "
          f"(수입 부가세 {sum(ln.vat_krw for ln in ok):,}원 포함)")
    out = BASE / "reports" / f"sourcing_{datetime.now():%Y%m%d_%H%M}.csv"
    write_csv(lines, [m["name"] for m in cfg["markets"].values()], out)
    print(f"리포트 저장: {out}\n")


if __name__ == "__main__":
    main()
