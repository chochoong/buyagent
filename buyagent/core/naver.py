"""네이버 검색어 트렌드 (NAVER API HUB) — 키워드별 월별 상대 검색량

※ 네이버 쇼핑 검색 API(상품·가격)는 2026-07-31 종료되어 대체 API가 없습니다.
   국내 판매가는 sourcing.csv 의 price_<마켓> 칸에 직접 입력하세요.

키 발급 (네이버 클라우드 플랫폼, https://www.ncloud.com)
  콘솔 → Services → Application Services → NAVER API HUB → Application 등록
  → '검색어 트렌드' API 선택 → [인증 정보]의 Client ID / Client Secret
  → buyagent/.env 의 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET (코드·로그·커밋에 쓰지 않음)

API 문서: https://api.ncloud-docs.com/docs/naver-api-hub-search-trend
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

from .http import post_json

HUB = "https://naverapihub.apigw.ntruss.com"


def env_conf(base: Path, conf: dict | None = None) -> dict:
    """.env 의 NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 를 conf 에 넣어 돌려줌 (api_base 등 다른 설정은 그대로)"""
    from .translate import load_dotenv
    load_dotenv(base / ".env")
    out = dict(conf or {})
    out["client_id"] = os.environ.get("NAVER_CLIENT_ID", "").strip()
    out["client_secret"] = os.environ.get("NAVER_CLIENT_SECRET", "").strip()
    return out


class Naver:
    def __init__(self, conf: dict):
        self.conf = conf
        self.base = conf.get("api_base", HUB).rstrip("/")

    def ready(self) -> bool:
        return bool(self.conf.get("client_id") and self.conf.get("client_secret"))

    def _headers(self) -> dict:
        return {"X-NCP-APIGW-API-KEY-ID": self.conf["client_id"],
                "X-NCP-APIGW-API-KEY": self.conf["client_secret"]}

    def trend(self, keywords: list[str], months: int = 12) -> dict[str, list[tuple[str, float]]]:
        """키워드별 [(월, 상대 검색량)]. 상대값은 한 번에 요청한 키워드(최대 5개) 안에서 최대값 = 100"""
        end = date.today().replace(day=1) - timedelta(days=1)    # 이번 달은 덜 끝났으니 지난달까지
        y, mth = end.year, end.month - months + 1
        while mth <= 0:
            y, mth = y - 1, mth + 12
        periods = []                                             # 검색량이 거의 없는 달은 응답에서 빠지므로 0으로 채움
        py, pm = y, mth
        for _ in range(months):
            periods.append(f"{py}-{pm:02d}-01")
            py, pm = (py + 1, 1) if pm == 12 else (py, pm + 1)
        out: dict[str, list[tuple[str, float]]] = {}
        for i in range(0, len(keywords), 5):
            body = {"startDate": periods[0], "endDate": end.isoformat(), "timeUnit": "month",
                    "keywordGroups": [{"groupName": k, "keywords": [k]} for k in keywords[i:i + 5]]}
            d = post_json(f"{self.base}/search-trend/v1/search", body, self._headers())
            for r in d.get("results", []):
                got = {x.get("period", "")[:7]: float(x.get("ratio", 0)) for x in r.get("data", [])}
                if got:
                    out[r["title"]] = [(p, got.get(p[:7], 0.0)) for p in periods]
        return out


def trend_change(series: list[float]) -> float | None:
    """최근 3개월 평균 ÷ 그 전 3개월 평균 - 1"""
    if len(series) < 6:
        return None
    prev, last = sum(series[-6:-3]) / 3, sum(series[-3:]) / 3
    return (last / prev - 1) if prev else None


def sparkline(series: list[float]) -> str:
    bars = "▁▂▃▄▅▆▇█"
    top = max(series) if series else 0
    return "".join(bars[min(7, int(v / top * 7))] if top else bars[0] for v in series)
