#!/usr/bin/env python3
"""
구매대행 모니터 v3 - 확장형

  python main.py                  # 실제 실행
  python main.py --demo           # API 키 없이 예시 데이터로 실행
  python main.py --bundle 5       # 5개 묶음 배송 기준으로 개당 원가 계산
  python main.py --only SBTQ041   # 특정 상품만 (ID 일부 일치)
  python main.py --check          # 설정·카탈로그 점검만
  python main.py --probe          # API 키 넣은 뒤 검색 1번으로 확인 (--probe "검색어")
  python main.py --test-alert     # 웹훅 테스트 메시지
  python main.py --no-alert       # 시험 실행: 웹훅 알림만 안 보냄 (조회·기록·리포트는 평소대로)
  python main.py --search "セイコー SBTQ041" --category watch
                                  # 직접 검색: products.csv 에 없어도 바로 최저가·원가·권장가 (→ reports/search.json)

  실행 결과는 reports/latest.json 에도 남음 (trade_hub 대시보드가 읽음)
  종료 코드: 0 = 정상 (일부 상품 차단·품절 포함), 1 = 실행 실패 (설정 오류·모든 조회 실패 등)

확장 방법
  - 상품 추가        → catalog/products.csv 에 한 줄 추가 (엑셀 편집 가능)
  - 신상 감시 추가   → catalog/discover.csv 에 한 줄 추가
  - 카테고리·규칙    → config/categories.toml
  - 새 쇼핑몰/국가   → sources/_template.py 복사 + settings.toml 섹션 추가
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

# Windows에서 출력을 파일로 넘기면(작업 스케줄러, > run.log) cp949로 인코딩돼서 이모지에서 죽음 → UTF-8 고정
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from core import translate                                  # noqa: E402
from core.config import load_config, load_glossary, load_specs, load_toml  # noqa: E402
from core.fx import FX                                     # noqa: E402
from core.models import Offer, Query, Result, Spec         # noqa: E402
from core.pricing import price_offer                       # noqa: E402
from core.report import fmt_result, send_webhook, write_csv, write_latest, write_search  # noqa: E402
from core.rules import category_label, check_spec, filter_offers, warnings_for  # noqa: E402
from core.shipping_rates import stale_messages              # noqa: E402
from core.pause import paused_since                         # noqa: E402
from core.storage import Store, alert_key                  # noqa: E402
from core.validate import ERROR, validate                  # noqa: E402
from sources import REGISTRY, build_sources                # noqa: E402


class Runner:
    def __init__(self, cfg: dict, demo: bool, bundle: int):
        self.cfg, self.demo, self.bundle = cfg, demo, bundle
        self.fx = FX(cfg, demo)
        self.sources = build_sources(cfg, demo)
        self.store = Store(BASE / ("history_demo.db" if demo else "history.db"))
        self.ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # -- 검색: 지정된 소스(없으면 전체)에서 모아오기 --
    def search_sources(self) -> list[str]:
        """sources 를 비운 상품이 쓰는 소스 (manual 처럼 이름을 적어야 쓰는 소스 제외)"""
        return [n for n, s in self.sources.items() if not s.explicit_only]

    def fetch(self, spec: Spec, newest: bool) -> tuple[list[Offer], list[str], list[str]]:
        """(오퍼, 조회 오류, 못 쓴 소스).
        조회 오류 = 이번에만 실패했을 수 있음(네트워크·키 만료) → 품절·원가 판단 보류
        못 쓴 소스 = 설정상 꺼져 있음 → 늘 같으니 남은 소스 결과로 판단"""
        offers: list[Offer] = []
        errors: list[str] = []
        skipped: list[str] = []
        if self.demo:
            names = list(self.sources)
        elif spec.sources:
            names = spec.sources
        else:
            names = self.search_sources()
            if not names:
                skipped.append("검색할 쇼핑몰 없음 (API 키 미입력 → config/settings.local.toml)")
        if newest:      # 최신순 정렬이 없는 소스(야후)는 신상 발견에서 제외
            names = [n for n in names if n not in self.sources or self.sources[n].supports_newest]
            if not names and not skipped:
                skipped.append("최신순 검색을 지원하는 소스 없음 (신상 발견은 rakuten 필요)")
        for n in names:
            src = self.sources.get(n)
            if src is None:
                skipped.append(f"소스 '{n}' 비활성/키 없음")
                continue
            try:
                offers += src.search(Query(spec.keyword, spec.jan, newest, spec.id))
            except Exception as e:
                errors.append(f"{n}: {e}")
        return offers, errors, skipped

    def priced(self, spec: Spec, offer: Offer) -> Result:
        r = Result(spec=spec)
        try:
            price_offer(self.cfg, self.fx, r, offer, self.bundle)
        except KeyError as e:
            r.offer, r.alerts = offer, [f"계산 불가: {e}"]
        r.alerts += warnings_for(self.cfg, offer.name) + offer.notes
        return r

    def cheapest(self, spec: Spec, offers: list[Offer]) -> Result:
        """배대지 요금·관부가세까지 넣은 '개당 원가'가 가장 낮은 오퍼.
        (현지가만 비교하면 일본·미국처럼 배송비가 다른 나라를 섞을 때 틀림)"""
        cands = [self.priced(spec, o) for o in offers]
        good = [r for r in cands if r.cost_krw > 0]
        return min(good, key=lambda r: r.cost_krw) if good else cands[0]

    def run_product(self, spec: Spec, track: bool = True) -> Result:
        res = Result(spec=spec)
        if reason := check_spec(self.cfg, spec):
            res.blocked_reason = reason
            return res
        offers, errors, skipped = self.fetch(spec, newest=False)
        ok = [o for o in filter_offers(self.cfg, offers, spec, res.excluded) if o.in_stock]
        if ok:
            stats = res.excluded
            res = self.cheapest(spec, ok)
            res.excluded = stats
            if not spec.trusted_shops and self.cfg["categories"][spec.category].get("check_shop", True):
                res.alerts.append(f"판매처 '{res.offer.shop}' 정품 취급처인지 확인 후 영수증 보관")
        if errors or (skipped and not offers):
            # 쇼핑몰 하나라도 조회에 실패하면 품절·재입고·원가 변동을 판단하지 않음
            # (실패한 쪽에만 재고가 있었으면 가짜 '품절' 알림이 나가고 다음 실행에 '재입고'가 나감)
            res.failed = True
            res.alerts.append("조회 실패: " + " / ".join(errors + skipped))
        else:
            if skipped:
                res.alerts.append("검색 못 한 쇼핑몰: " + " / ".join(skipped))
            if track:
                self.store.track(res, self.ts, self.cfg["alerts"]["price_change_rate"])
        return res

    def run_discover(self, spec: Spec) -> tuple[list[Result], str]:
        if reason := check_spec(self.cfg, spec):
            return [], f"건너뜀: {reason}"
        offers, errors, skipped = self.fetch(spec, newest=True)
        if (errors or skipped) and not offers:
            return [], "조회 실패: " + " / ".join(errors + skipped)
        stats: dict[str, int] = {}
        ok = [o for o in filter_offers(self.cfg, offers, spec, stats) if o.in_stock]
        first = self.store.is_first_watch(spec.name)
        new = [self.priced(spec, o) for o in ok
               if self.store.mark_seen(spec.name, o.key, o.name, self.ts) and (self.demo or not first)]
        msg = (f"첫 실행: 기존 {len(ok)}건 기록(다음부터 알림)" if first and not self.demo
               else f"신상 {len(new)}건")
        if stats:
            msg += " | 제외: " + ", ".join(f"{k} {v}" for k, v in stats.items())
        return new, f"검색 {len(offers)}건 → {msg}"


def warn_unknown_words(cfg: dict) -> None:
    if cfg["glossary_unknown"]:
        print("[경고] 단어장(config/glossary.toml)에 없는 한글 단어 → 일본어 상품명에 걸리지 않음: "
              + ", ".join(cfg["glossary_unknown"]), file=sys.stderr)


def check(cfg: dict, specs: list[Spec], all_specs: list[Spec]) -> int:
    """설정 점검. 오류가 있으면 1 반환 (작업 스케줄러·스크립트에서 실패로 인식)"""
    print("\n[설정 점검]")
    print("플러그인:", ", ".join(sorted(REGISTRY)))
    print("개인 설정:", cfg["_local_settings"] or "없음 (config/settings.local.toml.example 을 복사해서 만드세요)")
    for name, conf in cfg.get("sources", {}).items():
        cls = REGISTRY.get(name)
        state = "플러그인 없음" if not cls else ("준비됨" if cls(conf, cfg).ready() else "비활성/키 없음")
        print(f"  소스 {name}: {state}")
    for word, ja in cfg["glossary_used"].items():
        print(f"  단어장: {word} → {', '.join(ja)}")

    print("\n[감시 대상]")
    for s in specs:
        reason = check_spec(cfg, s)
        print(f"  {'신상' if s.discover else '상품'} {s.id}: {'⛔ ' + reason if reason else 'OK'}")

    issues = validate(cfg, all_specs, REGISTRY, BASE)     # ID 중복은 --only 와 상관없이 전체로 검사
    errors = [m for lv, m in issues if lv == ERROR]
    warns = [m for lv, m in issues if lv != ERROR]
    print("\n[문제 점검]")
    for m in errors:
        print(f"  ❌ {m}")
    for m in warns:
        print(f"  ⚠ {m}")
    print(f"  → 오류 {len(errors)}건, 주의 {len(warns)}건" + ("" if issues else " (문제 없음)"))
    return 1 if errors else 0


def test_alert(cfg: dict) -> int:
    """웹훅으로 테스트 메시지 1건 전송"""
    url = cfg["alerts"].get("webhook_url", "")
    if not url:
        print("webhook_url 이 비어 있음 → config/settings.local.toml 의 [alerts] webhook_url 에 입력하세요")
        return 1
    kind = "Discord" if "discord" in url else "Slack"
    ok = send_webhook(url, [f"테스트 알림입니다 ({datetime.now():%Y-%m-%d %H:%M})",
                            "• 이 메시지가 보이면 알림 설정 완료"])
    print(f"{kind} 웹훅 전송 {'성공 → 채널을 확인하세요' if ok else '실패 (위 오류 참고)'}")
    return 0 if ok else 1


def _raw_items(raw: dict) -> list[dict]:
    """API 응답 원문에서 상품 목록(Items, hits 등) 찾기"""
    for v in raw.values():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v
    return []


def probe(cfg: dict, specs: list[Spec], keyword: str) -> int:
    """API 키를 넣은 직후 확인용: 소스마다 검색 1번 → 응답·변환 결과 출력"""
    target = next((s for s in specs if not s.discover and s.keyword), None)
    q = Query(keyword or (target.keyword if target else "セイコー"),
              "" if keyword or not target else target.jan)
    print(f"\n[API 확인] 검색어: {q.jan or q.keyword}")
    fail = 0
    for name, conf in cfg.get("sources", {}).items():
        cls = REGISTRY.get(name)
        if not cls or name == "demo":
            continue
        src = cls(conf, cfg)
        print(f"\n■ {name}")
        if not src.ready():
            missing = [k for k in cls.required_keys if not conf.get(k)]
            print(f"  건너뜀: {'꺼져 있음' if not conf.get('enabled') else '키 미입력 ' + ', '.join(missing)}")
            continue
        try:
            offers = src.search(q)
        except Exception as e:
            fail += 1
            print(f"  ❌ 실패: {e}")
            continue
        items = _raw_items(src.last_raw)
        print(f"  ✅ 응답 OK: 원문 {len(items)}건 → 변환 {len(offers)}건")
        if items:
            print(f"  원문 필드: {', '.join(sorted(items[0]))}")
        for o in offers[:3]:
            print(f"  - {o.name[:50]}")
            print(f"    {o.price:,.0f} {o.currency} | 배송 {'무료' if o.shipping_known else '별도(가정)'}"
                  f" | {'재고있음' if o.in_stock else '품절'} | {o.shop} | JAN {o.jan or '-'}")
        if items and not offers:
            print("  ⚠ 원문은 있는데 변환 결과가 0건 → 필드 이름이 바뀌었을 수 있음 (위 원문 필드를 알려주세요)")
    return 1 if fail else 0


# ---------------------------------------------------------------- 실행 상태 알림
HEALTH_FILE = BASE / "health.json"
REMIND_HOURS = 24


def health_messages(problem: str, now: datetime | None = None, path: Path | None = None) -> list[str]:
    """실행 상태가 바뀔 때만 알림: 문제 발생·문제 변경 시 1번, 계속 실패 중이면 하루 1번, 해결되면 '복구' 1번"""
    now = now or datetime.now()
    path = path or HEALTH_FILE
    try:
        prev = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prev = {}
    key = alert_key(problem) if problem else ""
    msgs: list[str] = []
    state = {"key": key, "since": prev.get("since", ""), "sent": prev.get("sent", "")}
    if not problem:
        if prev.get("key"):
            msgs = [f"✅ 모니터 정상 복구 (실패 시작: {prev.get('since', '?')})"]
        state = {"key": "", "since": "", "sent": ""}
    else:
        ts = now.strftime("%Y-%m-%d %H:%M")
        if key != prev.get("key"):
            msgs = [f"🚨 모니터 실행 실패: {problem}"]
            state = {"key": key, "since": ts, "sent": ts}
        else:
            try:
                last = datetime.strptime(prev.get("sent", ""), "%Y-%m-%d %H:%M")
            except ValueError:
                last = datetime.min
            if (now - last).total_seconds() >= REMIND_HOURS * 3600:
                msgs = [f"🚨 아직 실패 중 ({prev.get('since', '?')}부터): {problem}"]
                state["sent"] = ts
    try:
        path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return msgs


def _fallback_webhook() -> str:
    """설정 파일을 못 읽어서 죽은 경우에도 실패 알림은 보내도록 웹훅 주소만 따로 찾음"""
    for name in ("settings.local.toml", "settings.toml"):
        try:
            url = load_toml(BASE / "config" / name).get("alerts", {}).get("webhook_url", "")
        except Exception:
            continue
        if url:
            return url
    return ""


# ---------------------------------------------------------------- 직접 검색
def model_words(keyword: str) -> list[str]:
    """검색어에서 모델명(영문+숫자 섞인 단어, 예: SBTQ041)만 골라 상품명 필수 단어로 씀
    → 시계줄·케이스 같은 다른 상품이 최저가로 뽑히는 것을 줄임"""
    return [w for w in keyword.split() if any(c.isdigit() for c in w) and any(c.isalpha() for c in w)]


TRANSLATE_CALL = None       # 테스트에서 가짜 번역 함수로 바꿈 (None = 실제 Claude 호출)
# --search --market 값 (trade_hub 사이트 선택과 같은 값). sources=None 이면 일반 검색 소스 전부(라쿠텐·야후)
SEARCH_MARKETS = {
    "rakuten": {"name": "라쿠텐(일본)", "lang": "ja", "sources": None},
    "aliexpress": {"name": "알리익스프레스(중국)", "lang": "en", "sources": ["aliexpress"]},
    "ebay": {"name": "이베이(미국)", "lang": "en", "sources": ["ebay"]},
}


def search(cfg: dict, args) -> int:
    """products.csv 에 등록하지 않고 한 번 검색. 가격 이력(history.db)·알림·health.json 은 건드리지 않음"""
    keyword = " ".join(args.search.split())
    cats = {k: v for k, v in cfg["categories"].items() if not k.startswith("_")}
    if not keyword:
        print("검색어를 넣으세요: python main.py --search \"セイコー SBTQ041\" --category watch", file=sys.stderr)
        return 2
    if args.category not in cats:
        print(f"--category 를 정하세요 (관세율·마진 기준·차단 규칙이 카테고리마다 다름). 가능한 값:\n  "
              + "\n  ".join(f"{k:<16} {v.get('name', '')}{'' if v.get('allowed') else ' (등록 금지)'}"
                             for k, v in cats.items()), file=sys.stderr)
        return 2
    mk = SEARCH_MARKETS.get(args.market)
    if mk is None:
        print(f"--market 은 {' / '.join(SEARCH_MARKETS)} 중 하나", file=sys.stderr)
        return 2
    original = keyword if translate.has_korean(keyword) else None
    if original:                        # 한글 검색어 → 사이트 언어 (실패하면 검색하지 않음, search.json 그대로)
        try:
            keyword = translate.to_lang(keyword, BASE, mk["lang"], load_glossary(BASE), call=TRANSLATE_CALL)
        except translate.TranslateError as err:
            print(str(err))
            return 2
        print(f"번역: {original} → {keyword}")
    must = [w for w in args.must.split("|") if w.strip()] if args.must else model_words(keyword)
    excl = ([w for w in args.exclude.split("|") if w.strip()] if args.exclude
            else list(cats[args.category].get("search_exclude", [])))
    if args.market != "rakuten" and not args.exclude:
        excl = []                       # 카테고리 search_exclude 는 일본어 단어 → 다른 언어 사이트엔 안 맞음
    spec = Spec(id="search", name=keyword, category=args.category, keyword=keyword, must_include=must,
                exclude=excl, sources=list(mk["sources"] or []))
    query = {"keyword": keyword, "keyword_original": original, "category": args.category,
             "market": args.market, "must_include": must, "exclude": spec.exclude}
    markets = list(cfg["markets"])
    out = BASE / "reports" / "search.json"
    run = Runner(cfg, args.demo, args.bundle)
    print(f"\n[직접 검색] {keyword}  ({mk['name']}, 카테고리: {category_label(cfg, args.category)}"
          + (f", 필수 단어: {'·'.join(must)}" if must else "") + ")")
    print(f"소스: {', '.join(spec.sources or run.search_sources() or run.sources) or '없음'}")
    missing = [n for n in spec.sources if n not in run.sources and not run.demo]
    if missing:                          # 키 없음 → 검색하지 않고 실패로 기록 (빈 결과를 '상품 없음'으로 오해하지 않게)
        keys = getattr(REGISTRY.get(missing[0]), "env_keys", ())
        problem = f"{mk['name']} 검색 불가: " + (f"키 없음 → buyagent/.env 에 {', '.join(keys)} 입력" if keys
                                                  else f"소스 {', '.join(missing)} 꺼짐 (settings.toml)")
        write_search(out, problem, query, markets, [], {}, 0)
        print(f"  ❌ {problem}")
        return 1

    if reason := check_spec(cfg, spec):
        res = Result(spec=spec, blocked_reason=reason)
        print("\n".join(fmt_result(res)))
        write_search(out, None, query, markets, [res], {}, 0)
        return 0
    offers, errors, skipped = run.fetch(spec, newest=False)
    excluded: dict[str, int] = {}
    ok = [o for o in filter_offers(cfg, offers, spec, excluded) if o.in_stock]
    results = sorted((r for r in (run.priced(spec, o) for o in ok) if r.cost_krw > 0), key=lambda r: r.cost_krw)
    if len(results) >= 3:       # 중간값보다 훨씬 싸면 부속품·다른 모델일 가능성 → 표시만 (빼지는 않음)
        mid = results[len(results) // 2].cost_krw
        for r in results:
            if r.cost_krw < mid * 0.4:
                r.alerts.insert(0, f"다른 결과 중간값(원가 {mid:,}원)보다 크게 쌈 → 부속품·다른 모델인지 상품명 확인")
    results = results[:max(1, args.top)]
    problem = " / ".join(errors + skipped) if (errors or skipped) and not offers else ""
    if errors and offers:
        for r in results:
            r.alerts.append("일부 쇼핑몰 조회 실패: " + " / ".join(errors))
    write_search(out, problem, query, markets, results, excluded, len(offers))

    if problem:
        print(f"  ❌ 조회 실패: {problem}")
    print(f"  검색 {len(offers)}건 → 조건 통과 {len(ok)}건"
          + (" | 제외: " + ", ".join(f"{k} {v}" for k, v in excluded.items()) if excluded else ""))
    for i, r in enumerate(results, 1):
        print(f"\n  {i}. {r.offer.name[:70]}")
        print("\n".join(fmt_result(r, "     ")))
        print(f"     {r.offer.url}")
    if not results and not problem:
        print("  조건에 맞는 재고 있는 판매처 없음 → --must 로 필수 단어를 바꾸거나 검색어를 바꿔 보세요")
    print(f"\n결과 저장: {out}")
    return 1 if problem else 0


# ---------------------------------------------------------------- 실행
def save_latest(error: str, demo: bool, markets: list[str], rows: list[tuple[str, Result]]) -> None:
    """reports/latest.json 덮어쓰기. 실패해도 감시 실행 자체는 계속"""
    try:
        write_latest(BASE / "reports" / "latest.json", error or None, demo, markets, rows)
    except Exception as e:
        print(f"[경고] latest.json 저장 실패: {type(e).__name__}: {e}", file=sys.stderr)


def run_all(cfg: dict, args, specs: list[Spec], all_specs: list[Spec]) -> str:
    """감시 1회 실행. 문제가 있으면 설명 문자열, 없으면 "" 반환"""
    problems: list[str] = []
    warn_unknown_words(cfg)
    n_err = sum(lv == ERROR for lv, _ in validate(cfg, all_specs, REGISTRY, BASE))
    if n_err:
        print(f"[경고] 설정 오류 {n_err}건 → python main.py --check 로 확인하세요", file=sys.stderr)
        problems.append(f"설정 오류 {n_err}건 → python main.py --check 로 확인")
    run = Runner(cfg, args.demo, args.bundle)
    if not run.sources:
        msg = "사용 가능한 소스가 없습니다. config/settings.local.toml 에 API 키를 넣거나 --demo로 실행하세요."
        print(msg, file=sys.stderr)
        problem = " / ".join(problems + [msg])
        save_latest(problem, args.demo, list(cfg["markets"]), [])
        return problem
    if not run.search_sources() and any(not s.sources for s in specs):
        # manual(수동 가격)만 준비됨 → sources 를 비운 상품은 하나도 검색되지 않음 (키 미입력·만료)
        msg = "검색할 쇼핑몰이 없습니다 → config/settings.local.toml 에 라쿠텐 키를 넣거나, 크롬을 설치해 [sources.rakuten_web] 을 켜세요."
        print(msg, file=sys.stderr)
        problems.append(msg)
    markets = [m["name"] for m in cfg["markets"].values()]
    print(f"\n환율: {run.fx.summary()}\n소스: {', '.join(run.sources)}")

    rows: list[tuple[str, Result]] = []
    notify: list[str] = []
    tried, failures = 0, []     # 전부 실패하면 키 만료·네트워크 문제일 가능성 → 실행 실패로 알림

    # 배대지 요금표 확인할 때가 됐으면 알림 (같은 상태면 한 번만)
    stale = stale_messages(cfg)
    for m in stale:
        print(f"[배대지 요금] {m}")
    stale_new = run.store.fresh_alerts("_shipping_rates", stale)
    if stale_new:
        notify += ["*배대지 요금*"] + [f"• {m}" for m in stale_new]

    print("\n" + "=" * 72 + "\n[고정 모니터링 상품]")
    for spec in (s for s in specs if not s.discover):
        try:
            r = run.run_product(spec)
        except Exception as e:          # 상품 하나가 실패해도 나머지는 계속
            r = Result(spec=spec, failed=True, alerts=[f"처리 중 오류: {type(e).__name__}: {e}"])
        if not r.blocked_reason:
            tried += 1
            if r.failed:
                failures.append(next((a for a in r.alerts if "실패" in a or "오류" in a), "조회 실패"))
        print(f"\n■ {spec.name}  ({category_label(cfg, spec.category)})")
        print("\n".join(fmt_result(r)))
        rows.append(("고정", r))
        # 품절·재입고·원가 변동은 항상, 나머지 경고는 지난번에 없던 것만 알림
        state = ([r.blocked_reason] if r.blocked_reason else []) + r.alerts
        msgs = r.events + run.store.fresh_alerts(spec.id, state, replace=not r.failed)
        if msgs:
            notify += [f"*{spec.name}*"] + [f"• {m}" for m in msgs]

    print("\n" + "=" * 72 + "\n[신상품 발견]")
    for spec in (s for s in specs if s.discover):
        try:
            new, msg = run.run_discover(spec)
        except Exception as e:
            new, msg = [], f"처리 중 오류: {type(e).__name__}: {e}"
        if not msg.startswith("건너뜀"):
            tried += 1
            if msg.startswith(("조회 실패", "처리 중 오류")):
                failures.append(msg)
        print(f"\n▶ {spec.name}  ({msg})")
        for r in new:
            print(f"  ✨ {r.offer.name}")
            print("\n".join(fmt_result(r, "     ")))
            rows.append(("신상", r))
            q = r.quotes[0] if r.quotes else None
            notify.append(f"✨ [{spec.name}] {r.offer.name} — {r.offer.total:,.0f} {r.offer.currency}"
                          + (f", 권장가 {q.recommended_price:,}원" if q else "") + f"\n   {r.offer.url}")

    out = BASE / "reports" / f"report_{datetime.now():%Y%m%d_%H%M}.csv"
    write_csv(rows, markets, out)
    print(f"\n리포트 저장: {out}\n")
    if getattr(args, "no_alert", False):
        print(f"[--no-alert] 웹훅 알림 {len([m for m in notify if not m.startswith('*')])}건 보내지 않음")
    elif not args.demo:
        send_webhook(cfg["alerts"].get("webhook_url", ""), notify)
    if tried and len(failures) == tried:
        problems.append(f"감시 {tried}건 모두 조회 실패 (API 키 만료·네트워크 확인) → {failures[0]}")
    problem = " / ".join(problems)
    save_latest(problem, args.demo, list(cfg["markets"]), rows)
    return problem


def main() -> None:
    ap = argparse.ArgumentParser(description="구매대행 모니터 v3")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--bundle", type=int, default=0, help="묶음 배송 수량 (개당 원가 계산)")
    ap.add_argument("--only", default="", help="ID에 이 문자열이 들어간 상품만")
    ap.add_argument("--settings", default="", help="settings.toml 경로")
    ap.add_argument("--check", action="store_true", help="설정 점검만")
    ap.add_argument("--test-alert", action="store_true", help="웹훅으로 테스트 메시지 전송")
    ap.add_argument("--probe", nargs="?", const="", default=None, metavar="검색어",
                    help="API 키 확인: 소스마다 검색 1번 해서 응답 출력")
    ap.add_argument("--no-alert", action="store_true",
                    help="시험 실행: 웹훅 알림만 보내지 않음 (가격 조회·history.db·리포트·latest.json 은 평소대로, health.json 은 그대로)")
    ap.add_argument("--search", default=None, metavar="검색어",
                    help="직접 검색: products.csv 에 없어도 최저가·원가·권장가 확인 (→ reports/search.json)")
    ap.add_argument("--market", default="rakuten", choices=list(SEARCH_MARKETS),
                    help="--search 할 사이트 (rakuten: 일본어 검색, aliexpress·ebay: 영어 검색·키 필요)")
    ap.add_argument("--category", default="", help="--search 의 카테고리 (categories.toml 키, 예: watch)")
    ap.add_argument("--must", default="", help="--search 상품명 필수 단어, 여러 개는 | 로 (기본: 검색어 속 모델명)")
    ap.add_argument("--exclude", default="", help="--search 제외 단어, 여러 개는 | 로 (기본: 카테고리의 search_exclude)")
    ap.add_argument("--top", type=int, default=10, help="--search 결과 몇 개까지 (개당 원가 낮은 순)")
    args = ap.parse_args()

    cfg: dict | None = None
    try:
        cfg = load_config(BASE, Path(args.settings) if args.settings else None)
        all_specs = load_specs(BASE, cfg["markets"])
        specs = [s for s in all_specs if args.only in s.id]
        if args.check:
            sys.exit(check(cfg, specs, all_specs))
        if args.test_alert:
            sys.exit(test_alert(cfg))
        if args.probe is not None:
            sys.exit(probe(cfg, specs, args.probe))
        if args.search is not None:
            sys.exit(search(cfg, args))
        print(f"===== {datetime.now():%Y-%m-%d %H:%M:%S} 실행 =====")
        since = None if args.demo else paused_since(BASE)
        if since:                       # 화면에서 '감시 전체 멈춤' → 조회·알림 없이 끝 (실패 아님)
            print(f"[멈춤] 감시 전체 멈춤 중 ({since}부터) → 조회·알림 안 함. 화면에서 '감시 다시 시작'을 누르세요.")
            sys.exit(0)
        problem = run_all(cfg, args, specs, all_specs)
    except Exception as e:              # 프로그램 전체가 죽는 경우 (설정 파일 문법 오류 등)
        traceback.print_exc()
        problem = f"프로그램 오류 {type(e).__name__}: {e}"
        if not (args.check or args.test_alert or args.probe is not None or args.search is not None):
            save_latest(problem, args.demo, list((cfg or {}).get("markets", {})), [])

    if args.demo or args.no_alert:      # 예시·시험 실행은 실행 상태(health.json)·실패 알림을 건드리지 않음
        if problem:
            print(f"[실패] {problem}", file=sys.stderr)
        sys.exit(1 if problem else 0)
    msgs = health_messages(problem)
    if msgs:
        url = (cfg or {}).get("alerts", {}).get("webhook_url", "") or _fallback_webhook()
        send_webhook(url, msgs)
    if problem:
        print(f"[실패] {problem}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
