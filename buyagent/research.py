#!/usr/bin/env python3
"""
상품 발굴: 후보 키워드의 네이버 검색 추이 비교 (NAVER API HUB 키 필요)

  python research.py 키캡 손목받침 장패드 기계식키보드
  python research.py --file catalog/research.txt     # 한 줄에 키워드 하나
  python research.py 키캡 --months 24

보여주는 것
  - 최근 12개월 월별 검색량 그래프 (계절성: 언제 많이 찾는지)
  - 최근 3개월이 그 전 3개월보다 늘었는지
  - 같이 조회한 키워드끼리 검색량 크기 비교 (5개씩 묶어서 조회 → 같은 묶음 안에서만 비교 가능)

※ 네이버 쇼핑 검색 API가 2026-07-31 종료되어 가격·판매처 수는 가져올 수 없습니다.
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
from core.naver import Naver, sparkline, trend_change      # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="상품 발굴 (네이버 검색 추이)")
    ap.add_argument("keywords", nargs="*", help="조사할 키워드")
    ap.add_argument("--file", default="", help="키워드 목록 파일 (한 줄에 하나)")
    ap.add_argument("--months", type=int, default=12, help="조회 기간(개월)")
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
                 "발급: 네이버 클라우드 플랫폼 콘솔 → NAVER API HUB → Application 등록 (검색어 트렌드)")
    try:
        data = nv.trend(keywords, args.months)
    except Exception as e:
        sys.exit(f"검색 추이 조회 실패: {e}")

    print()
    rows = []
    for i, kw in enumerate(keywords):
        series = data.get(kw, [])
        vals = [v for _, v in series]
        if i % 5 == 0 and len(keywords) > 5:
            print(f"--- 묶음 {i // 5 + 1} (이 묶음 안에서만 검색량 크기 비교 가능) ---")
        if not vals:
            print(f"■ {kw}: 데이터 없음 (검색량이 너무 적거나 오류)\n")
            rows.append([kw, "", "", "", "", ""])
            continue
        ch = trend_change(vals)
        peak = series[vals.index(max(vals))][0][:7]
        avg = sum(vals) / len(vals)
        arrow = "" if ch is None else ("▲" if ch > 0.05 else "▼" if ch < -0.05 else "→")
        print(f"■ {kw}")
        print(f"  {sparkline(vals)}  ({series[0][0][:7]} ~ {series[-1][0][:7]})")
        print(f"  평균 {avg:.0f} · 최고 {peak}"
              + ("" if ch is None else f" · 최근 3개월 {arrow} {ch:+.0%}") + "\n")
        rows.append([kw, f"{avg:.1f}", peak, "" if ch is None else f"{ch:+.0%}", (i // 5) + 1,
                     " ".join(f"{v:.0f}" for v in vals)])

    out = BASE / "reports" / f"research_{datetime.now():%Y%m%d_%H%M}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["키워드", "평균 검색량(상대)", "최고 달", "최근3개월 변화", "비교 묶음", "월별 검색량"])
        w.writerows(rows)
    print(f"리포트 저장: {out}\n")


if __name__ == "__main__":
    main()
