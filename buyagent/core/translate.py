"""직접 검색어 번역: 한글이 들어 있으면 라쿠텐에서 통하는 일본어 검색어로 바꿈

  · 한글이 없으면(일본어·모델명만) 그대로 → 비용 없음
  · 먼저 config/glossary.toml 단어장을 적용 (단어 단위, 첫 번째 일본어 표현). 한글이 다 바뀌면 API 호출 안 함
  · 남은 한글은 Claude Haiku 로 한 번 번역 (검색 1번에 1원 미만)
  · 같은 검색어(공백 정리 후)는 data/translate_cache.json 에서 다시 씀 (git 제외)
  · 키: 환경변수 또는 buyagent/.env 의 ANTHROPIC_API_KEY. 없으면 실패 → 사람이 일본어로 직접 입력

번역 함수(call)는 바꿔 끼울 수 있음 → 테스트는 실제 API 를 부르지 않음
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

MODEL = "claude-haiku-5-5"
MAX_LEN = 100
HANGUL = re.compile(r"[가-힣ㄱ-ㆎ]")
FAIL = "번역 실패: 일본어로 직접 입력해 주세요"
_LOCK = threading.Lock()

SYSTEM = ("You convert Korean shopping search queries into the Japanese query a shopper would type on Rakuten Ichiba. "
          "Use the brand and product names as they are written in the Japanese market (katakana or the official name). "
          "Keep model numbers, sizes and Latin letters exactly as given. Do not add words that are not in the query. "
          "Reply with the Japanese query only — one line, no quotes, no explanation.")


class TranslateError(RuntimeError):
    pass


def has_korean(text: str | None) -> bool:
    return bool(HANGUL.search(text or ""))


def load_dotenv(path: Path) -> None:
    """KEY=VALUE 줄만 읽어 환경변수에 없을 때만 넣음 (이미 설정된 값을 덮어쓰지 않음)"""
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        if k and k not in os.environ:
            os.environ[k] = v


def call_claude(query: str, base: Path) -> str:
    """실제 번역 (Anthropic SDK). 키가 없거나 SDK 가 없으면 TranslateError"""
    load_dotenv(base / ".env")
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise TranslateError(f"{FAIL} (ANTHROPIC_API_KEY 없음 → buyagent/.env 에 넣으면 한글 검색 가능)")
    try:
        import anthropic
    except ImportError as err:
        raise TranslateError(f"{FAIL} (anthropic 패키지 없음 → pip install anthropic)") from err
    client = anthropic.Anthropic(api_key=key, max_retries=2, timeout=30)
    resp = client.messages.create(model=MODEL, max_tokens=200, system=SYSTEM,
                                  messages=[{"role": "user", "content": query}])
    return "".join(b.text for b in resp.content if b.type == "text")


def apply_glossary(query: str, glossary: dict[str, list[str]]) -> str:
    """단어장에 있는 한글 단어(띄어쓰기 단위)를 첫 번째 일본어 표현으로 바꿈"""
    out = []
    for w in query.split():
        hit = glossary.get(w.replace(" ", ""))
        out.append(hit[0] if hit and has_korean(w) else w)
    return " ".join(out)


def clean(raw: str | None) -> str:
    """모델 답 검증: 비었거나 / 여러 줄이거나 / 한글이 남았거나 / 너무 길면 실패"""
    text = (raw or "").strip()
    if "\n" in text:
        raise TranslateError(f"{FAIL} (번역 결과가 여러 줄)")
    text = " ".join(text.strip("\"'「」『』“”").split())
    if not text or len(text) > MAX_LEN or has_korean(text):
        raise TranslateError(f"{FAIL} (번역 결과가 이상함: {text[:40]!r})")
    return text


def _cache_path(base: Path) -> Path:
    return base / "data" / "translate_cache.json"


def _load(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".translate_", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def to_japanese(query: str, base: Path, glossary: dict[str, list[str]] | None = None,
                call: Callable[[str, Path], str] | None = None) -> str:
    """한글이 섞인 검색어 → 일본어 검색어. 한글이 없으면 그대로. 실패하면 TranslateError"""
    query = " ".join((query or "").split())
    if not has_korean(query):
        return query
    path = _cache_path(base)
    with _LOCK:
        cached = _load(path).get(query)
    if cached:
        return cached
    pre = apply_glossary(query, glossary or {})
    if not has_korean(pre):
        out = pre                                   # 단어장만으로 끝남 → API 호출 없음
    else:
        try:
            out = clean((call or call_claude)(pre, base))
        except TranslateError:
            raise
        except Exception as err:                    # 네트워크·API 오류
            raise TranslateError(f"{FAIL} ({type(err).__name__})") from err
    with _LOCK:
        cache = _load(path)
        cache[query] = out
        _save(path, cache)
    return out
