"""상품명 한국어 풀이: 일본어·중국어·영어 상품명 옆에 괄호로 붙일 한국어 (화면 표시용)

- 한글이 있거나 모델 번호뿐인 이름(GAC-2500-3AJF 등)은 번역하지 않음
- 번역은 Claude Haiku 한 번에 여러 개 (JSON 배열), 결과는 data/name_ko.json 에 저장해 다시 씀 (git 제외)
- 키: 환경변수 또는 buyagent/.env 의 ANTHROPIC_API_KEY. 없거나 실패하면 괄호 없이 원래 이름만 보임
- 가격·판정에는 쓰지 않음 (보기 편하라고 붙이는 설명)
"""
from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Callable
from pathlib import Path

from .translate import HANGUL, MODEL, _load, _save, load_dotenv

CJK = re.compile(r"[぀-ヿ㐀-鿿豈-﫿]")       # 히라가나·가타카나·한자
WORD = re.compile(r"[A-Za-z][a-z]{2,}")           # 소문자가 섞인 영어 단어 (모델 번호만 있는 이름은 제외)
MAX_NAME = 200
MAX_KO = 120
BATCH = 30
_LOCK = threading.Lock()

SYSTEM = ("You translate online-shop product titles (Japanese, Chinese or English) into short natural Korean so a Korean "
          "reseller can tell what the product is. Keep brand names in common Korean spelling, keep model numbers "
          "(e.g. SBTQ041, GAC-2500-3AJF) exactly, drop shop promotions such as coupons, points, 送料無料, 【...】 sale tags. "
          "Reply with a JSON array of Korean strings only, same order and same length as the input array.")


def needs(name: str | None) -> bool:
    t = name or ""
    return bool(t.strip()) and not HANGUL.search(t) and bool(CJK.search(t) or WORD.search(t))


def path(base: Path) -> Path:
    return base / "data" / "name_ko.json"


def lookup(base: Path, names) -> dict[str, str]:
    with _LOCK:
        cache = _load(path(base))
    return {n: cache[n] for n in names if n in cache}


def missing(base: Path, names) -> list[str]:
    have = lookup(base, names)
    return list(dict.fromkeys(n for n in names if needs(n) and n not in have))


def ready(base: Path) -> bool:
    """번역 키가 있는지 (없으면 번역을 시작하지 않음 → 키를 넣으면 서버를 다시 켜지 않아도 바로 시작)"""
    load_dotenv(base / ".env")
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def call_claude(names: list[str], base: Path) -> str:
    load_dotenv(base / ".env")
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY 없음")
    import anthropic
    client = anthropic.Anthropic(api_key=key, max_retries=2, timeout=60)
    resp = client.messages.create(model=MODEL, max_tokens=4000, system=SYSTEM,
                                  messages=[{"role": "user", "content": json.dumps(names, ensure_ascii=False)}])
    return "".join(b.text for b in resp.content if b.type == "text")


def parse(raw: str, n: int) -> list[str | None]:
    """모델 답 → 한국어 목록. 형식이 틀리면 ValueError, 한 줄씩 이상하면 그 줄만 None"""
    text = (raw or "").strip()
    m = re.search(r"\[.*\]", text, re.S)
    out = json.loads(m.group(0) if m else text)
    if not isinstance(out, list) or len(out) != n:
        raise ValueError("번역 개수가 다름")
    good = []
    for v in out:
        v = " ".join(str(v or "").split())
        good.append(v if v and HANGUL.search(v) and len(v) <= MAX_KO else None)
    return good


def fill(base: Path, names, call: Callable[[list[str], Path], str] | None = None) -> int:
    """캐시에 없는 이름만 번역해서 저장. 저장한 개수. 실패한 묶음은 건너뜀 (예외 안 냄)"""
    todo = missing(base, names)
    added = 0
    for i in range(0, len(todo), BATCH):
        part = todo[i:i + BATCH]                     # 저장 키는 원래 이름, 보내는 건 앞 MAX_NAME 글자
        send = [n[:MAX_NAME] for n in part]
        try:
            got = parse(call(send, base) if call else call_claude(send, base), len(part))
        except Exception:
            continue
        with _LOCK:
            cache = _load(path(base))
            for k, v in zip(part, got):
                if v:
                    cache[k] = v
                    added += 1
            _save(path(base), cache)
    return added
