#!/usr/bin/env python3
"""
상품 발굴: 후보 키워드의 국내 시장을 네이버로 조사 (네이버 키 필요)

  python research.py 키캡 손목받침 장패드
  python research.py --file catalog/research.txt     # 한 줄에 키워드 하나
  python research.py 키캡 --no-trend                 # 검색 추이(데이터랩) 빼고

보여주는 것
  - 가격대     : 정확도순 상위 상품의 최저 / 하위 25% / 중간 가격
  - 경쟁       : 네이버 쇼핑 전체 상품 수, 상위 상품의 판매처 수
  - 해외직구   : 해외직구(구매대행) 상품 비율. 높으면 국내 재고 판매(사입)로 배송이 빠른 게 경쟁력
  - 검색 추이  : 최근 3개월 검색량이 그 전 3개월보다 늘었는지 (데이터랩)
결과: 화면 + reports/research_날짜.csv
"""
from __future__ import annotations

import argparse
import csv
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

from core.config import load_config                       # noqa: E402
from core.naver import Market, Naver, trend_change        # noqa: E402


def research(nv: Naver, keywords: list[str], trend: bool) -> list[tuple[Market | None, float | None, str]]:
    trends: dict[str, list[float]] = {}
    trend_err = ""
    if trend:
        try:
            trends = nv.trend(keywords)
        except Exception as e:
            trend_err = f"검색 추이 조회 실패 (데이터랩 API 사용 설정 확인): {e}"
    out = []
    for kw in keywords:
        try:
            m: Market | None = nv.market(kw)
            err = trend_err
        except Exception as e:
            m, err = None, f"조회 실패: {e}"
        out.append((m, trend_change(trends.get(kw, [])), err))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="상품 발굴 (네이버 시장 조사)")
    ap.add_argument("keywords", nargs="*", help="조사할 키워드")
    ap.add_argument("--file", default="", help="키워드 목록 파일 (한 줄에 하나)")
    ap.add_argument("--no-trend", action="store_true", help="검색 추이(데이터랩) 생략")
    args = ap.parse_args()

    keywords = list(args.keywords)
    if args.file:
        lines = Path(args.file).read_text(encoding="utf-8-sig").splitlines()
        keywords += [x.strip() for x in lines if x.strip() and not x.strip().startswith("#")]
    keywords = list(dict.fromkeys(keywords))
    if not keywords:
        sys.exit("키워드를 넣어 주세요. 예: python research.py 키캡 손목받침")

    cfg = load_config(BASE)
    nv = Naver(cfg.get("naver", {}))
    if not nv.ready():
        sys.exit("네이버 키 없음 → config/settings.local.toml 의 [naver] client_id / client_secret 을 넣으세요.\n"
                 "발급: https://developers.naver.com → Application → 애플리케이션 등록")

    rows = research(nv, keywords, not args.no_trend)
    print()
    for m, ch, err in rows:
        kw = m.query if m else "?"
        if m is None:
            print(f"■ {err}\n")
            continue
        print(f"■ {kw}")
        if m.items:
            print(f"  가격대: 최저 {m.stat('min'):,} · 하위25% {m.stat('p25'):,} · 중간 {m.stat('median'):,} · "
                  f"최고 {m.stat('max'):,}원")
        print(f"  경쟁: 상품 {m.total:,}개 · 상위 {len(m.items)}개 중 판매처 {m.malls}곳 · 해외직구 {m.cb_share:.0%}")
        if ch is not None:
            arrow = "▲" if ch > 0.05 else "▼" if ch < -0.05 else "→"
            print(f"  검색 추이: 최근 3개월 {arrow} {ch:+.0%} (그 전 3개월 대비)")
        if err:
            print(f"  ⚠ {err}")
        for it in m.items[:3]:
            print(f"   - {it.price:,}원 {it.title[:45]} [{it.mall}]")
        print()

    out = BASE / "reports" / f"research_{datetime.now():%Y%m%d_%H%M}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["키워드", "최저가", "하위25%", "중간가", "최고가", "전체상품수", "상위판매처수",
                    "해외직구비율", "검색추이(3개월)", "대표상품", "메모"])
        for m, ch, err in rows:
            if m is None:
                w.writerow(["", "", "", "", "", "", "", "", "", "", err])
                continue
            w.writerow([m.query, m.stat("min"), m.stat("p25"), m.stat("median"), m.stat("max"), m.total,
                        m.malls, f"{m.cb_share:.0%}", "" if ch is None else f"{ch:+.0%}",
                        m.items[0].title if m.items else "", err])
    print(f"리포트 저장: {out}\n")


if __name__ == "__main__":
    main()
