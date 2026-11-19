#!/usr/bin/env python3
"""
buyagent 자체 화면 — trade_hub 없이도 혼자 쓰는 구매대행 작업 화면

  python web.py              # http://127.0.0.1:8766 (내 PC에서만 열림)
  python web.py --port 9000
  python web.py --no-open    # 브라우저 자동으로 열지 않기

할 수 있는 것
  · 직접 검색   : 검색어 + 카테고리 → main.py --search (최저가·원가·권장가, 감시·알림에 영향 없음)
  · 감시에 추가 : 방금 검색한 조건을 catalog/products.csv 에 한 줄 추가 → 다음 감시 실행부터 매시간 추적
  · 감시 켜기/끄기, 시험 실행(main.py --no-alert: 웹훅만 안 보냄)
  · 최근 감시 결과(reports/latest.json), 감시 상품, 수동 가격 보기

다른 곳과의 관계
  · 계산은 모두 main.py 가 한다 (이 화면은 main.py 를 실행하고 결과 파일을 읽을 뿐)
  · 시장조사 → 공급처 발굴(2번) → export-buyagent 로 들어온 상품도 같은 products.csv 라 여기서 함께 보임
  · trade_hub 대시보드는 같은 결과 파일(reports/*.json)을 읽기만 함 → 둘 다 켜 둬도 됨

보안: 127.0.0.1 에만 열고 Host 헤더 확인(DNS 리바인딩 방지), 실행 요청(POST)은
      X-Buyagent: 1 헤더가 있어야 받음(다른 사이트에서 몰래 보내는 요청 차단). 결제·주문 기능 없음.
"""
from __future__ import annotations

import argparse
import csv
import html
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import webbrowser
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from core.pause import paused_since, set_paused
from core.shipping_rates import SIZES, checked_dates, stale_messages, update_rates

BASE = Path(__file__).resolve().parent
HOST = "127.0.0.1"
DEFAULT_PORT = 8766
MAX_QUERY = 100
JOB_TIMEOUT = {"search": 5 * 60, "run": 30 * 60}     # 초
# 직접 검색 사이트 (main.py SEARCH_MARKETS 와 같은 값) → 감시 추가 때 products.csv sources 칸
SEARCH_MARKETS = {"rakuten": "라쿠텐(일본·일본어로 검색)", "aliexpress": "알리익스프레스(중국·영어로 검색)",
                  "ebay": "이베이(미국·영어로 검색)"}
MARKET_SOURCES = {"rakuten": "", "aliexpress": "aliexpress", "ebay": "ebay"}
PRODUCT_COLS = ["id", "name", "category", "keyword", "jan", "must_include", "exclude", "trusted_shops", "sources",
                "shipping_profile", "bundle_size", "my_price_coupang", "my_price_naver", "active", "memo",
                "hub_product_id"]
FROM_SOURCING = "2번 공급처 발굴에서 가져옴"
FROM_SEARCH = "직접 검색에서 추가"

JOB: dict = {}                    # 지금(또는 마지막) 작업 하나 — 검색·실행은 한 번에 하나만
LOCK = threading.Lock()
CSV_LOCK = threading.Lock()


# ---------------------------------------------------------------- 파일 읽기
def e(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def read_json(path: Path) -> tuple[dict | None, str]:
    if not path.exists():
        return None, ""
    try:
        return json.loads(path.read_text(encoding="utf-8")), ""
    except (OSError, ValueError) as err:
        return None, f"{path.name} 을(를) 읽지 못함: {type(err).__name__}"


def read_csv(path: Path) -> tuple[list[str], list[dict]]:
    if not path.exists():
        return [], []
    with open(path, newline="", encoding="utf-8-sig") as f:
        r = csv.DictReader(f)
        return list(r.fieldnames or []), list(r)


def categories(base: Path = BASE) -> dict[str, dict]:
    try:
        with open(base / "config" / "categories.toml", "rb") as f:
            cats = tomllib.load(f).get("categories", {})
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in cats.items() if not k.startswith("_")}


def settings(base: Path = BASE) -> dict:
    try:
        with open(base / "config" / "settings.toml", "rb") as f:
            return tomllib.load(f)
    except (OSError, ValueError):
        return {}


SIZE_NAMES = {"small": "small (~0.5kg)", "medium": "medium (~2kg)", "large": "large (~5kg)"}


def rates_section(cfg: dict) -> tuple[str, str]:
    """(경고 배너, '배대지 요금' 본문). [shipping_checked] 에 적힌 나라만 고칠 수 있음"""
    stale = stale_messages(cfg)
    banner = "".join(f'<p class="warn small">⚠ {e(m)}</p>' for m in stale)
    dates = checked_dates(cfg)
    rows = []
    for origin, d in dates.items():
        table_ = cfg.get("shipping_profiles", {}).get(origin, {})
        name = cfg.get("origins", {}).get(origin, {}).get("name", origin)
        inputs = "".join(
            f'<label class="small">{e(SIZE_NAMES[k])} <input type="number" name="{k}" min="0" max="500000" step="100" '
            f'required value="{e(table_.get(k, ""))}" style="width:110px;min-width:0;flex:none">원</label> '
            for k in SIZES)
        rows.append(f'<form class="row rates" data-origin="{e(origin)}"><b>{e(name)}</b> {inputs}'
                    f'<span class="small {"muted" if d else "warn"}">마지막 확인: {e(d or "기록 없음 (추정값)")}</span>'
                    f'<button type="submit" class="sm">저장 (오늘 확인함)</button></form>')
    max_days = cfg.get("shipping_checked", {}).get("max_days", 30)
    body = ("".join(rows) or '<p class="muted">settings.toml [shipping_checked] 에 나라가 없어요.</p>') + (
        f'<p class="small muted">쓰는 배대지의 공식 요금표(항공)에서 0.5kg·2kg·5kg 요금을 넣고 저장하세요. '
        f'저장하면 확인 날짜가 오늘로 바뀌고, {e(max_days)}일이 지나면 다시 알려 줘요. '
        f'비교 사이트(짐스캐너 등)는 보고 참고만 하고 자동으로 가져오지 않아요(이용약관상 영리 목적 수집 금지).</p>')
    return banner, body


def rates_request(body: dict, base: Path = BASE) -> tuple[int, dict]:
    origin = str(body.get("origin") or "")
    if origin not in checked_dates(settings(base)):
        return 400, {"error": "settings.toml [shipping_checked] 에 없는 나라입니다."}
    try:
        fees = {k: int(str(body.get(k, "")).strip()) for k in SIZES}
        update_rates(base / "config" / "settings.toml", origin, fees)
    except ValueError as err:
        return 400, {"error": str(err) or "요금은 숫자(원)로 넣어 주세요."}
    return 200, {"ok": True}


TASK_NAME = "buyagent"      # schedule.bat 이 등록하는 작업 스케줄러 이름 (매시간)


def schedule_status() -> dict:
    """작업 스케줄러 등록 여부와 다음 실행 시각 (읽기만 함, 등록·변경은 schedule.bat 으로 사람이)"""
    if os.name != "nt":
        return {"registered": False, "next": ""}
    try:
        p = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "CSV", "/NH"],
                           capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return {"registered": False, "next": ""}
    if p.returncode != 0:
        return {"registered": False, "next": ""}
    text = p.stdout.decode("cp949", errors="replace")
    row = next(csv.reader(io.StringIO(text.strip().splitlines()[0])), []) if text.strip() else []
    return {"registered": True, "next": row[1] if len(row) > 1 else ""}


def watch_status(base: Path, latest: dict | None) -> str:
    """감시 상품 위: 자동 실행 주기·마지막 감시·전체 멈춤 버튼"""
    since = paused_since(base)
    st = schedule_status()
    if st["registered"]:
        nxt = f", 다음 {e(st['next'])}" if st["next"] else ""
        sched = f"자동 실행: <b>매시간</b> (작업 스케줄러 '{TASK_NAME}'{nxt})"
    else:
        sched = ('자동 실행: <span class="warn">등록 안 됨</span> → 정기 감시·디스코드 알림을 받으려면 '
                 '<code>schedule.bat</code> 을 더블클릭하세요')
    last = (latest or {}).get("run_at") or ""
    last_txt = f' · 마지막 감시 {e(last[:16].replace("T", " "))}' + (" (예시 데이터)" if (latest or {}).get("demo") else "") if last else " · 감시 실행 기록 없음"
    if since:
        state = f'<span class="warn">⏸ 감시 전체 멈춤 중 ({e(since)}부터) — 정기 실행이 와도 조회·알림을 안 해요</span>'
        btn = '<button class="sm" data-act="pause" data-paused="0">▶ 감시 다시 시작</button>'
    else:
        state = '<span class="ok">● 감시 중</span>'
        btn = '<button class="sm" data-act="pause" data-paused="1">⏸ 감시 전체 멈춤</button>'
    return f'<div class="row between small"><span>{state} · {sched}{last_txt}</span>{btn}</div>'


def pause_request(body: dict, base: Path = BASE) -> tuple[int, dict]:
    if not isinstance(body.get("paused"), bool):
        return 400, {"error": "paused 값(true/false)이 필요합니다."}
    set_paused(base, body["paused"])
    return 200, {"paused": body["paused"]}


def local_ship_text(o: dict) -> str:
    """현지(판매처 → 배대지) 배송비 설명"""
    if not o:
        return ""
    if not o.get("shipping_known"):
        return "현지배송 미확인"
    s = o.get("shipping") or 0
    return "현지배송 무료" if not s else f"현지배송 {money(s, o.get('currency'))} 포함"


def market_names(base: Path = BASE) -> dict[str, str]:
    try:
        with open(base / "config" / "settings.toml", "rb") as f:
            return {k: v.get("name", k) for k, v in tomllib.load(f).get("markets", {}).items()}
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------- products.csv 쓰기
def write_csv_atomic(path: Path, header: list[str], rows: list[dict]) -> None:
    """엑셀에서도 열리게 UTF-8(BOM)·CRLF. 임시 파일 → 이름 바꾸기 (main.py 가 읽는 중이어도 반쯤 쓴 파일을 안 봄)"""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=header, extrasaction="ignore", lineterminator="\r\n")
    w.writeheader()
    w.writerows(rows)
    fd, tmp = tempfile.mkstemp(prefix=".products_", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as f:
            f.write(buf.getvalue())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def make_id(keyword: str, must: list[str], taken: set[str]) -> str:
    base = "-".join(re.sub(r"[^a-z0-9]+", "-", w.lower()).strip("-") for w in must) \
        or re.sub(r"[^a-z0-9]+", "-", keyword.lower()).strip("-")[:30] \
        or f"item-{datetime.now():%Y%m%d%H%M}"
    pid, n = base, 2
    while pid in taken:
        pid, n = f"{base}-{n}", n + 1
    return pid


def suggest_must(q: dict) -> str:
    """감시 추가 때 '꼭 들어갈 단어' 기본값: 검색 때 쓴 필수 단어, 없으면 검색어 속 영문 대문자·숫자 코드(SBTR, SBTQ041)"""
    must = [w for w in q.get("must_include") or [] if w]
    if not must:
        must = [w for w in (q.get("keyword") or "").split()
                if len(w) >= 3 and re.fullmatch(r"[A-Z0-9][A-Z0-9-]*", w) and any(c.isalpha() for c in w)]
    return "|".join(must)


def parse_must(text: str) -> list[str]:
    words = [w.strip() for w in str(text or "").split("|") if w.strip()]
    if len(words) > 5 or any(len(w) > 30 for w in words):
        raise ValueError("꼭 들어갈 단어는 5개까지, 단어마다 30자 이내로 넣어 주세요.")
    return words


def add_watch(base: Path, search: dict, name: str = "", must: str | None = None) -> tuple[int, dict]:
    """마지막 직접 검색 조건을 감시 목록에 추가. 이미 같은 검색어가 있으면 거절
    must = 상품명에 꼭 들어가야 할 단어('|' 구분). None 이면 검색 때 조건 그대로"""
    q = (search or {}).get("query") or {}
    keyword, cat = (q.get("keyword") or "").strip(), (q.get("category") or "").strip()
    if not keyword or not cat:
        return 400, {"error": "먼저 검색을 해 주세요."}
    try:
        must_words = list(q.get("must_include") or []) if must is None else parse_must(must)
    except ValueError as err:
        return 400, {"error": str(err)}
    path = base / "catalog" / "products.csv"
    with CSV_LOCK:
        header, rows = read_csv(path)
        header = header or list(PRODUCT_COLS)
        for col in ("memo", "hub_product_id"):
            if col not in header:
                header.append(col)
        same = next((r for r in rows if (r.get("keyword") or "").strip() == keyword), None)
        if same:
            return 409, {"error": f"이미 감시 중인 검색어입니다 (ID {same.get('id')})."}
        pid = make_id(keyword, must_words, {(r.get("id") or "").strip() for r in rows})
        rows.append({
            "id": pid, "name": (name or keyword).strip()[:100], "category": cat, "keyword": keyword,
            "must_include": "|".join(must_words), "exclude": "|".join(q.get("exclude") or []),
            "sources": MARKET_SOURCES.get(q.get("market") or "rakuten", ""),
            "my_price_coupang": "0", "my_price_naver": "0", "active": "Y",
            "memo": f"{FROM_SEARCH} {date.today().isoformat()}",
        })
        write_csv_atomic(path, header, rows)
    return 200, {"id": pid, "must_include": must_words}


def toggle_watch(base: Path, pid: str) -> tuple[int, dict]:
    path = base / "catalog" / "products.csv"
    with CSV_LOCK:
        header, rows = read_csv(path)
        row = next((r for r in rows if (r.get("id") or "").strip() == pid), None)
        if row is None:
            return 404, {"error": f"ID {pid} 가 products.csv 에 없습니다."}
        on = (row.get("active") or "Y").strip().upper() not in ("N", "0", "FALSE")
        row["active"] = "N" if on else "Y"
        if "active" not in header:
            header.append("active")
        write_csv_atomic(path, header, rows)
    return 200, {"id": pid, "active": row["active"]}


# ---------------------------------------------------------------- 작업 실행 (main.py)
def job_command(kind: str, arg: tuple = ()) -> list[str]:
    if kind == "search":
        q, cat, market = arg
        return [sys.executable, "main.py", f"--search={q}", f"--category={cat}",
                f"--market={market}"]   # --옵션=값: 검색어가 옵션으로 오인 안 됨
    if kind == "run":
        return [sys.executable, "main.py", "--no-alert"]
    raise ValueError(kind)


def start_job(kind: str, arg: tuple, label: str, base: Path = BASE) -> tuple[int, dict]:
    with LOCK:
        if JOB.get("status") == "running":
            return 409, {"error": f"'{JOB['label']}' 실행 중입니다. 끝난 뒤 다시 눌러 주세요."}
        JOB.clear()
        JOB.update(kind=kind, label=label, status="running", lines=[], started=time.time(), ended=None)
        job = dict(JOB)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen(job_command(kind, arg), cwd=base, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                            encoding="utf-8", errors="replace")
    threading.Thread(target=_watch, args=(proc, JOB_TIMEOUT[kind]), daemon=True).start()
    return 200, job_view(job)


def _watch(proc, timeout: int) -> None:
    timer = threading.Timer(timeout, proc.kill)
    timer.start()
    for line in proc.stdout:
        if line.strip():
            with LOCK:
                JOB["lines"] = (JOB["lines"] + [line.rstrip()])[-8:]
    proc.stdout.close()
    code = proc.wait()
    timer.cancel()
    with LOCK:
        JOB.update(status="done" if code == 0 else "failed", ended=time.time(), exit_code=code)


def job_view(j: dict) -> dict:
    if not j:
        return {}
    sec = int((j.get("ended") or time.time()) - j["started"])
    return {"label": j["label"], "status": j["status"], "lines": j["lines"],
            "elapsed": f"{sec // 60}분 {sec % 60}초" if sec >= 60 else f"{sec}초"}


def search_request(body: dict, base: Path = BASE) -> tuple[int, dict]:
    q = " ".join(str(body.get("query") or "").split())
    cat = str(body.get("category") or "")
    market = str(body.get("market") or "rakuten")
    if market not in SEARCH_MARKETS:
        return 400, {"error": f"사이트는 {' / '.join(SEARCH_MARKETS)} 중 하나입니다."}
    if not q:
        return 400, {"error": "검색어를 입력해 주세요. (일본 쇼핑몰 검색어, 예: セイコー SBTQ041)"}
    if len(q) > MAX_QUERY:
        return 400, {"error": f"검색어는 {MAX_QUERY}자 이내로 입력해 주세요."}
    cats = categories(base)
    if not cats.get(cat, {}).get("allowed"):
        return 400, {"error": "카테고리를 골라 주세요. 카테고리마다 배송비·마진 기준이 달라 원가와 권장가가 바뀝니다."}
    return start_job("search", (q, cat, market),
                     f"검색: {q} ({SEARCH_MARKETS[market]} · {cats[cat].get('name', cat)})", base)


# ---------------------------------------------------------------- 화면
def link(url, label) -> str:
    url = str(url or "")
    if not url.startswith(("http://", "https://")):        # javascript: 같은 주소는 링크로 걸지 않음
        return e(label)
    return f'<a href="{e(url)}" target="_blank" rel="noopener noreferrer">{e(label)}</a>'


def money(v, cur="") -> str:
    if not isinstance(v, (int, float)):
        return '<span class="muted">–</span>'
    fmt = "{:,.0f}" if cur in ("", "JPY", "KRW") else "{:,.2f}"
    return fmt.format(v) + (f" {e(cur)}" if cur else "원")


def clip(t, n=80) -> str:
    t = str(t or "")
    return e(t) if len(t) <= n else f'<span title="{e(t)}">{e(t[:n])}…</span>'


def table(heads: list[str], rows: list[list[str]]) -> str:
    th = "".join(f"<th>{h}</th>" for h in heads)
    trs = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f'<div class="tw"><table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table></div>'


def cost_cell(it: dict) -> str:
    """개당 원가 + 그 안에 들어간 배대지 비용 (현지 무료배송이어도 배대지 비용은 따로 듦)"""
    cost = it.get("cost_krw")
    if not isinstance(cost, (int, float)):
        return money(cost)
    fwd, prof = it.get("forwarding_krw"), it.get("shipping_profile")
    parts = []
    if isinstance(fwd, (int, float)):
        parts.append(f"배대지 {money(fwd)} 포함" + (f" ({e(prof)})" if prof else "") if fwd else "배대지 없음 (직배송)")
    ls = local_ship_text(it.get("best_offer") or {})
    if ls:
        parts.append(ls)
    return money(cost) + (f'<div class="small muted">{" · ".join(parts)}</div>' if parts else "")


STATUS = {"ok": ("판매 가능", "ok"), "blocked": ("차단", "bad"), "no_offer": ("판매처 없음", "warn"), "error": ("오류", "bad")}


def result_rows(items: list[dict], markets: list[str], names: dict[str, str], numbered: bool) -> tuple[list[str], list]:
    heads = (["#"] if numbered else []) + ["상품", "판정", "최저가 판매처", "현지가", "개당 원가"] \
        + [f"{names.get(m, m)} 권장가" for m in markets] + ["경고·알림"]
    rows = []
    for i, it in enumerate(items, 1):
        label, cls = STATUS.get(it.get("status"), (it.get("status") or "–", "muted"))
        o = it.get("best_offer") or {}
        ship = ' <span class="tag unk">배송비 미확인</span>' if o and o.get("shipping_known") is False else ""
        quotes = {q.get("market"): q for q in it.get("quotes") or []}
        cells = []
        for m in markets:
            q = quotes.get(m)
            cells.append(f'{money(q.get("recommended_price"))}<div class="small muted">마진 {money(q.get("margin"))}</div>'
                         if q else '<span class="muted">–</span>')
        why = it.get("blocked_reason")
        rows.append(([str(i)] if numbered else []) + [
            clip(it.get("name") or it.get("id"), 70) + (f'<div class="small muted">{e(it.get("id"))}</div>' if not numbered else ""),
            f'<span class="{cls}">{e(label)}</span>' + (f'<div class="small">{clip(why, 50)}</div>' if why else ""),
            link(o.get("url"), o.get("shop") or o.get("source") or "판매처") if o else '<span class="muted">–</span>',
            (money(o.get("price"), o.get("currency")) + ship) if o else "",
            cost_cell(it), *cells,
            clip(" / ".join((it.get("events") or []) + (it.get("warnings") or [])), 90)])
    return heads, rows


def render(base: Path = BASE) -> str:
    cats = categories(base)
    names = market_names(base)
    search, s_err = read_json(base / "reports" / "search.json")
    latest, l_err = read_json(base / "reports" / "latest.json")
    _, products = read_csv(base / "catalog" / "products.csv")
    _, prices = read_csv(base / "catalog" / "manual_prices.csv")

    banner, rbody = rates_section(settings(base))

    # --- 직접 검색
    last = (search or {}).get("query") or {}
    # 카테고리는 매번 직접 고름 (기본값·지난 검색값을 미리 골라 두면 노트가 '시계' 기준 배송비·마진으로 계산되는 일이 생김)
    opts = '<option value="" selected disabled>카테고리 선택</option>' + "".join(
        f'<option value="{e(k)}">{e(v.get("name", k))}</option>' for k, v in cats.items() if v.get("allowed"))
    m_opts = "".join(f'<option value="{e(k)}"{" selected" if k == (last.get("market") or "rakuten") else ""}>'
                     f'{e(v)}</option>' for k, v in SEARCH_MARKETS.items())
    form = (f'<form id="search" class="row"><input name="query" required maxlength="{MAX_QUERY}" '
            f'placeholder="검색어 (예: セイコー SBTQ041 · 한글은 사이트 언어로 자동 번역)" '
            f'value="{e(last.get("keyword_original") or last.get("keyword") or "")}">'
            f'<select name="market" title="검색할 사이트 (한글은 그 사이트 언어로 번역)">{m_opts}</select>'
            f'<select name="category" required title="배송비·관세율·마진 기준·차단 규칙이 카테고리마다 다름">{opts}</select>'
            f'<button type="submit">🔎 검색</button></form>'
            '<p class="small muted">고른 사이트에서 찾아 개당 원가가 낮은 순으로 보여 줘요 (30초~1분). 한글은 라쿠텐=일본어, 알리·이베이=영어로 자동 번역해요(ANTHROPIC_API_KEY 필요). 알리·이베이는 API 키가 있어야 해요. 모델명(예: SBTQ041)을 넣으면 '
            '그 단어가 들어간 상품만 남겨 시계줄 같은 부속품을 걸러요. 감시 목록·가격 이력·알림에는 영향 없어요.</p>')
    if s_err:
        sbody = f'<p class="bad">{e(s_err)}</p>'
    elif not search:
        sbody = '<p class="muted">아직 검색한 결과가 없어요.</p>'
    else:
        heads, rows = result_rows(search.get("items") or [], search.get("markets") or [], names, True)
        excl = search.get("excluded") or {}
        watched = any((p.get("keyword") or "").strip() == last.get("keyword") for p in products)
        add = ('<span class="muted small">이 검색어는 이미 감시 중</span>' if watched else
               f'<button data-act="add" data-name="{e(last.get("keyword_original") or last.get("keyword"))}" '
               f'data-must="{e(suggest_must(last))}">'
               '＋ 이 검색을 감시에 추가</button>')
        orig = last.get("keyword_original")
        sbody = (f'<div class="row between"><div class="small">「{e(last.get("keyword"))}」'
                 + (f' <span class="muted">← {e(orig)} (자동 번역)</span>' if orig else "") + " · "
                 f'{e(cats.get(last.get("category"), {}).get("name", last.get("category")))} · '
                 f'{e((search.get("run_at") or "")[:16].replace("T", " "))} · 쇼핑몰 결과 {e(search.get("found", 0))}건'
                 + (f' · <span class="bad">실패: {e(search.get("error"))}</span>' if not search.get("ok") else "")
                 + f'</div>{add}</div>'
                 + (table(heads, rows) if rows else '<p class="muted">조건에 맞는 재고 있는 판매처가 없어요.</p>')
                 + (f'<p class="small muted">걸러낸 결과: {e(", ".join(f"{k} {v}건" for k, v in excl.items()))}</p>'
                    if excl else ""))

    # --- 최근 감시 결과
    if l_err:
        lbody = f'<p class="bad">{e(l_err)}</p>'
    elif not latest:
        lbody = '<p class="muted">아직 감시를 실행한 적이 없어요. [시험 실행]을 누르거나 python main.py 를 실행하세요.</p>'
    else:
        heads, rows = result_rows(latest.get("items") or [], latest.get("markets") or [], names, False)
        lbody = (f'<p class="small">{e((latest.get("run_at") or latest.get("generated_at") or "")[:16].replace("T", " "))} 실행'
                 + (" · 예시 데이터(--demo)" if latest.get("demo") else "")
                 + (f' · <span class="bad">실패: {e(latest.get("error"))}</span>' if not latest.get("ok") else "")
                 + "</p>" + (table(heads, rows) if rows else '<p class="muted">결과 없음</p>'))

    # --- 감시 상품
    prow = []
    for p in products:
        on = (p.get("active") or "Y").strip().upper() not in ("N", "0", "FALSE")
        memo = p.get("memo") or ""
        tag = ('<span class="tag pipe">공급처 발굴에서</span> ' if FROM_SOURCING in memo else
               '<span class="tag srch">직접 검색에서</span> ' if FROM_SEARCH in memo else "")
        hub = (p.get("hub_product_id") or "").strip()
        prow.append([e(p.get("id")), clip(p.get("name"), 50) + (f' <span class="small muted">#{e(hub)}</span>' if hub else ""),
                     e(cats.get(p.get("category"), {}).get("name", p.get("category"))), clip(p.get("keyword"), 40),
                     e(p.get("sources") or "기본"),
                     f'<button class="sm" data-act="toggle" data-id="{e(p.get("id"))}">{"켜짐 ✓" if on else "꺼짐"}</button>',
                     tag + clip(memo, 40)])
    pbody = watch_status(base, latest) + (
        table(["ID", "상품", "카테고리", "검색어", "판매처 소스", "감시", "메모"], prow) if prow else '<p class="muted">없음</p>')

    # --- 수동 가격
    mrow = []
    for r in prices:
        ship = (r.get("shipping") or "").strip()
        mrow.append([e(r.get("id")), link(r.get("url"), r.get("shop") or "판매처"), f'{e(r.get("price"))} {e(r.get("currency"))}',
                     e(ship) if ship else '<span class="tag unk">미확인</span>',
                     "있음" if (r.get("in_stock") or "Y").strip().upper() not in ("N", "0", "FALSE") else '<span class="bad">품절</span>',
                     e(r.get("checked")), clip(r.get("memo"), 50)])
    mbody = table(["상품 ID", "판매처", "가격", "현지 배송비", "재고", "확인 날짜", "메모"], mrow) if mrow else '<p class="muted">없음</p>'

    return PAGE.format(
        css=CSS, js=JS, form=form, sbody=sbody, lbody=lbody, pbody=pbody, mbody=mbody, banner=banner, rbody=rbody,
        n_products=len(products), n_prices=len(prices), base=e(base))


CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--muted:#6b7280;--line:#e3e6eb;--acc:#2f6fed;--ok:#16803c;--warn:#b45309;--bad:#c62828}
@media (prefers-color-scheme:dark){:root{--bg:#14171c;--card:#1c2027;--fg:#e6e8eb;--muted:#9aa3ae;--line:#2c323b;--acc:#6b9bff;--ok:#4cc27a;--warn:#f0a447;--bad:#ff6b6b}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,"Malgun Gothic",sans-serif}
header{padding:14px 20px;border-bottom:1px solid var(--line);background:var(--card);display:flex;gap:12px;align-items:center;flex-wrap:wrap}
header h1{font-size:18px;margin:0}main{max-width:1280px;margin:0 auto;padding:16px}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:16px}
h2{font-size:16px;margin:0 0 8px}.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.between{justify-content:space-between;margin-bottom:8px}
input,select,button{font:inherit;padding:7px 10px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--fg)}
input{flex:1;min-width:220px}button{background:var(--acc);color:#fff;border-color:var(--acc);cursor:pointer}
button.sm{padding:2px 8px;font-size:12px;background:var(--bg);color:var(--fg);border-color:var(--line)}
button:disabled{opacity:.5;cursor:wait}.tw{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px}
th,td{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;vertical-align:top}th{color:var(--muted);font-weight:600;white-space:nowrap}
a{color:var(--acc)}.small{font-size:12px}.muted{color:var(--muted)}.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.tag{display:inline-block;font-size:11px;padding:0 6px;border-radius:9px;border:1px solid var(--line)}.unk{color:var(--warn);border-color:var(--warn)}
.pipe{color:var(--acc);border-color:var(--acc)}.srch{color:var(--ok);border-color:var(--ok)}
#job{position:fixed;right:16px;bottom:16px;max-width:460px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;box-shadow:0 4px 18px rgba(0,0,0,.15)}
#job pre{margin:6px 0 0;font-size:11px;white-space:pre-wrap;max-height:160px;overflow:auto;color:var(--muted)}
"""

JS = """
function api(path, body) {
  return fetch(path, {method: body ? "POST" : "GET",
    headers: body ? {"Content-Type": "application/json", "X-Buyagent": "1"} : {},
    body: body ? JSON.stringify(body) : undefined}).then(function (r) {
      return r.json().then(function (d) { if (!r.ok) throw new Error(d.error || r.status); return d; }); });
}
function esc(t) { var d = document.createElement("div"); d.textContent = t == null ? "" : String(t); return d.innerHTML; }
var box = document.getElementById("job"), timer = null, watching = false;
function poll() {
  api("/api/job").then(function (j) {
    if (!j.label) { box.hidden = true; return; }
    var st = {running: "⏳ 실행 중", done: "✅ 완료", failed: "❌ 실패"}[j.status] || j.status;
    box.hidden = false;
    box.innerHTML = "<b>" + esc(j.label) + "</b> <span class='small'>" + st + " · " + esc(j.elapsed) +
      "</span><pre>" + esc(j.lines.join("\\n")) + "</pre>";
    document.querySelectorAll("button").forEach(function (b) { b.disabled = j.status === "running" && b.dataset.act !== "toggle"; });
    if (j.status === "running") { watching = true; timer = setTimeout(poll, 1500); }
    else if (watching) { watching = false; location.reload(); }
  }).catch(function () { timer = setTimeout(poll, 4000); });
}
document.getElementById("search").addEventListener("submit", function (ev) {
  ev.preventDefault();
  var f = new FormData(ev.target);
  api("/api/search", {query: f.get("query"), category: f.get("category"), market: f.get("market")}).then(poll).catch(function (err) { alert(err.message); });
});
document.querySelectorAll("form.rates").forEach(function (f) {
  f.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var d = new FormData(f), body = {origin: f.dataset.origin};
    ["small", "medium", "large"].forEach(function (k) { body[k] = d.get(k); });
    if (!confirm("배대지 요금을 저장할까요? 다음 감시부터 모든 상품의 개당 원가·권장가가 이 값으로 계산돼요.")) return;
    api("/api/rates", body).then(function () { location.reload(); }).catch(function (err) { alert(err.message); });
  });
});
document.addEventListener("click", function (ev) {
  var b = ev.target.closest("button[data-act]");
  if (!b) return;
  if (b.dataset.act === "add") {
    var name = prompt("감시 목록에 넣을 상품 이름 (한국어로 알아보기 쉽게)", b.dataset.name);
    if (name === null) return;
    var must = prompt("상품명에 꼭 들어가야 할 단어 (모델명·시리즈명, 여러 개는 | 로 구분)\\n" +
                      "비워 두면 시곗줄·케이스·다른 모델이 최저가로 잡힐 수 있어요.", b.dataset.must || "");
    if (must === null) return;
    if (!must.trim() && !confirm("꼭 들어갈 단어 없이 추가할까요? (캐릭터 상품처럼 모델명이 없는 경우만 권장)")) return;
    api("/api/add", {name: name, must: must}).then(function () { location.reload(); }).catch(function (err) { alert(err.message); });
  } else if (b.dataset.act === "pause") {
    var on = b.dataset.paused === "1";
    if (!confirm(on ? "감시를 전체 멈출까요? 정기 실행이 와도 상품 조회·디스코드 알림을 하지 않아요. (상품별 켜짐/꺼짐은 그대로)"
                    : "감시를 다시 시작할까요? 다음 정기 실행부터 켜진 상품을 조회하고 알림을 보내요.")) return;
    api("/api/pause", {paused: on}).then(function () { location.reload(); }).catch(function (err) { alert(err.message); });
  } else if (b.dataset.act === "toggle") {
    api("/api/toggle", {id: b.dataset.id}).then(function () { location.reload(); }).catch(function (err) { alert(err.message); });
  } else if (b.dataset.act === "run") {
    if (!confirm("감시 목록 전체를 지금 실행할까요? 가격 이력·리포트는 평소처럼 남고, 웹훅 알림만 보내지 않습니다.\\n" +
                 "(이번 실행에서 생긴 알림은 다음 정식 실행에서도 다시 오지 않아요)")) return;
    api("/api/run", {}).then(poll).catch(function (err) { alert(err.message); });
  }
});
poll();
"""

PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>buyagent 구매대행</title>
<style>{css}</style></head><body>
<header><h1>🛒 buyagent 구매대행</h1><span class="small muted">{base}</span>
<span style="flex:1"></span><button data-act="run" title="main.py --no-alert">▶ 감시 시험 실행 (알림 없이)</button></header>
<main>
{banner}<section><h2>직접 검색</h2>{form}{sbody}</section>
<section><h2>최근 감시 결과</h2>{lbody}</section>
<section><h2>감시 상품 <span class="small muted">{n_products}개 · catalog/products.csv</span></h2>
<p class="small muted">시장조사 → 공급처 발굴(2번)에서 넘어온 상품과 직접 검색에서 추가한 상품이 함께 있어요. 세부 조건(JAN·필수 단어·판매처)은 CSV에서 고쳐요.</p>{pbody}</section>
<section><h2>배대지 요금 <span class="small muted">개당 원가에 들어가는 배대지·국제배송비 · config/settings.toml</span></h2>{rbody}</section>
<section><h2>수동 가격 <span class="small muted">{n_prices}줄 · catalog/manual_prices.csv</span></h2>{mbody}</section>
</main><div id="job" hidden></div>
<script>{js}</script></body></html>"""


# ---------------------------------------------------------------- 서버
class Handler(BaseHTTPRequestHandler):
    server_version = "buyagent"
    base = BASE

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = (json.dumps(body, ensure_ascii=False) if ctype.startswith("application/json") else body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self) -> bool:
        port = self.server.server_address[1]
        return (self.headers.get("Host") or "").lower() in (f"127.0.0.1:{port}", f"localhost:{port}")

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "허용되지 않은 Host"})
        path = urlparse(self.path).path
        if path == "/":
            return self._send(200, render(self.base), "text/html; charset=utf-8")
        if path == "/api/job":
            with LOCK:
                return self._send(200, job_view(dict(JOB)))
        return self._send(404, {"error": "없는 주소"})

    def do_POST(self):
        length = min(int(self.headers.get("Content-Length") or 0), 20_000)
        raw = self.rfile.read(length)                 # 거절하더라도 본문은 먼저 읽음 (안 읽고 닫으면 연결이 끊김)
        if not self._host_ok():
            return self._send(403, {"error": "허용되지 않은 Host"})
        if self.headers.get("X-Buyagent") != "1" or "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._send(403, {"error": "buyagent 화면에서만 실행할 수 있습니다"})
        try:
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._send(400, {"error": "요청 형식 오류"})
        path = urlparse(self.path).path
        if path == "/api/search":
            code, out = search_request(body, self.base)
        elif path == "/api/run":
            code, out = start_job("run", (), "감시 시험 실행 (알림 없이)", self.base)
        elif path == "/api/add":
            code, out = add_watch(self.base, read_json(self.base / "reports" / "search.json")[0] or {},
                                  str(body.get("name") or ""),
                                  None if body.get("must") is None else str(body.get("must")))
        elif path == "/api/pause":
            code, out = pause_request(body, self.base)
        elif path == "/api/rates":
            code, out = rates_request(body, self.base)
        elif path == "/api/toggle":
            code, out = toggle_watch(self.base, str(body.get("id") or ""))
        else:
            code, out = 404, {"error": "없는 주소"}
        return self._send(code, out)


def make_server(port: int = DEFAULT_PORT, base: Path = BASE) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"base": base})
    srv = ThreadingHTTPServer((HOST, port), handler)
    srv.daemon_threads = True
    return srv


def main() -> None:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(description="buyagent 자체 화면")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-open", action="store_true", help="브라우저 자동으로 열지 않기")
    args = ap.parse_args()
    try:
        srv = make_server(args.port)
    except OSError:
        print(f"포트 {args.port} 를 쓸 수 없습니다 (이미 실행 중이면 http://{HOST}:{args.port} 로 열면 됩니다)")
        sys.exit(1)
    url = f"http://{HOST}:{args.port}"
    print(f"buyagent 화면: {url}  (끄려면 Ctrl+C)")
    if not args.no_open:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
