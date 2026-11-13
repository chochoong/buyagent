"""HTTP 유틸 (표준 라이브러리만 사용)

- min_interval: 같은 호스트에 연속 호출할 때 최소 간격(초). 라쿠텐·야후는 초당 1회 제한이 있음
- retries     : 429·5xx·네트워크 오류일 때 재시도 횟수 (대기 시간은 2초, 4초, ... 로 늘어남)
"""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

UA = {"User-Agent": "buyagent/0.3"}
RETRY_CODES = {429, 500, 502, 503, 504}
MAX_WAIT = 30.0

_last_call: dict[str, float] = {}


def _throttle(host: str, min_interval: float) -> None:
    if min_interval <= 0:
        return
    wait = _last_call.get(host, 0.0) + min_interval - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_call[host] = time.monotonic()


def _retry_after(e: urllib.error.HTTPError, attempt: int) -> float:
    ra = (e.headers or {}).get("Retry-After", "")
    delay = float(ra) if ra.strip().isdigit() else 2.0 ** (attempt + 1)
    return min(delay, MAX_WAIT)


def get_json(url: str, params: dict | None = None, headers: dict | None = None,
             timeout: int = 15, min_interval: float = 0.0, retries: int = 2) -> dict:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    base = url.split("?")[0]          # 오류 메시지에 API 키가 찍히지 않도록 쿼리 제거
    host = urllib.parse.urlsplit(url).netloc
    for attempt in range(retries + 1):
        _throttle(host, min_interval)
        req = urllib.request.Request(url, headers={**UA, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "ignore")[:200]
            if e.code in RETRY_CODES and attempt < retries:
                time.sleep(_retry_after(e, attempt))
                continue
            raise RuntimeError(f"HTTP {e.code} {base}: {body}") from e
        except (urllib.error.URLError, TimeoutError, socket.timeout) as e:
            if attempt < retries:
                time.sleep(2.0 ** (attempt + 1))
                continue
            raise RuntimeError(f"연결 실패 {base}: {getattr(e, 'reason', e)}") from e
    raise RuntimeError(f"요청 실패 {base}")   # 도달하지 않음


def post_json(url: str, payload: dict, headers: dict | None = None, timeout: int = 15) -> dict:
    """JSON POST. 응답 본문이 JSON이면 돌려줌 (웹훅처럼 본문이 없으면 {})"""
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json", **UA, **(headers or {})},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} {url.split('?')[0]}: "
                           f"{e.read().decode('utf-8', 'ignore')[:200]}") from e
    try:
        return json.loads(body) if body.strip() else {}
    except ValueError:
        return {}
