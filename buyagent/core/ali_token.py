"""알리 Open Platform 토큰 (드롭쉬핑 상품 검색용) — 저장·자동 갱신·만료 알림

- 토큰 파일: buyagent/data/aliexpress_token.json (git 제외). 처음 발급은 사람이 data/get_token.py 로
  (인증 주소에서 받은 code + App Secret 입력 → access_token 24시간, refresh_token 48시간)
- access_token 만료 REFRESH_BEFORE 전부터 refresh_token 으로 새로 받음 (/auth/token/refresh)
  → 매시간 감시(main.py)가 돌 때마다 확인하므로 PC 가 켜져 있으면 계속 이어짐
- refresh_token 까지 끝나면 사람이 code 를 다시 받아야 함 → 감시 알림(디스코드)·검색 오류로 알림
- 토큰·App Secret 값은 화면·로그·알림에 쓰지 않음
- 상품 조회에만 씀. 주문 API 는 부르지 않음
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

REST = "https://api-sg.aliexpress.com/rest"
REFRESH_PATH = "/auth/token/refresh"
REFRESH_BEFORE = 6 * 3600          # access_token 만료 6시간 전부터 갱신
WARN_BEFORE = 12 * 3600            # refresh_token 만료 12시간 전부터 경고 (갱신이 계속 실패할 때)
REISSUE = "data/get_token.py 로 새 code 를 받아 다시 발급"


class TokenError(RuntimeError):
    pass


def path(base: Path) -> Path:
    return base / "data" / "aliexpress_token.json"


def load(base: Path) -> dict | None:
    try:
        d = json.loads(path(base).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) and d.get("access_token") else None
    except (OSError, ValueError):
        return None


def _save(base: Path, data: dict) -> None:
    p = path(base)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".ali_token_", suffix=".tmp", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _ms(v) -> float:
    try:
        return int(v) / 1000
    except (TypeError, ValueError):
        return 0.0


def sign_system(secret: str, api_path: str, params: dict) -> str:
    """시스템 API(/auth/...) 서명: API 경로 + 이름순 '이름+값' → HMAC-SHA256 → 대문자 hex"""
    text = api_path + "".join(f"{k}{params[k]}" for k in sorted(params) if k != "sign")
    return hmac.new(secret.encode(), text.encode(), hashlib.sha256).hexdigest().upper()


def post_refresh(params: dict) -> dict:
    """실제 갱신 요청 (테스트에서는 바꿔 끼움)"""
    req = urllib.request.Request(REST + REFRESH_PATH, data=urllib.parse.urlencode(params).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode())
        except ValueError:
            return {"code": f"HTTP {e.code}"}


def refresh(base: Path, app_key: str, secret: str, now: float | None = None,
            post: Callable[[dict], dict] | None = None) -> dict:
    """refresh_token 으로 새 토큰 받아 저장. 실패하면 TokenError (파일은 그대로)"""
    now = time.time() if now is None else now
    tok = load(base)
    if not tok or not tok.get("refresh_token"):
        raise TokenError(f"알리 토큰 없음 → {REISSUE}")
    if _ms(tok.get("refresh_token_valid_time")) and _ms(tok["refresh_token_valid_time"]) <= now:
        raise TokenError(f"알리 토큰 갱신 기한 지남 → {REISSUE}")
    params = {"app_key": app_key, "refresh_token": tok["refresh_token"], "sign_method": "sha256",
              "timestamp": str(int(now * 1000))}
    params["sign"] = sign_system(secret, REFRESH_PATH, params)
    try:
        got = (post or post_refresh)(params)
    except Exception as err:                      # 네트워크 오류
        raise TokenError(f"알리 토큰 갱신 실패 ({type(err).__name__})") from err
    if not isinstance(got, dict) or not got.get("access_token"):
        code = got.get("code") if isinstance(got, dict) else "?"
        msg = (got.get("message") or got.get("msg") or "") if isinstance(got, dict) else ""
        raise TokenError(f"알리 토큰 갱신 실패 {code} {msg}".strip())
    new = {**tok, **got, "refreshed_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))}
    _save(base, new)
    return new


def access_token(base: Path, app_key: str, secret: str, now: float | None = None,
                 post: Callable[[dict], dict] | None = None) -> str:
    """쓸 수 있는 access_token (만료가 가까우면 먼저 갱신). 못 구하면 TokenError"""
    now = time.time() if now is None else now
    tok = load(base)
    if not tok:
        raise TokenError(f"알리 토큰 없음 → {REISSUE}")
    exp = _ms(tok.get("expire_time"))
    if exp and exp - now < REFRESH_BEFORE:
        try:
            tok = refresh(base, app_key, secret, now, post)
        except TokenError:
            if exp > now:                         # 갱신은 실패했지만 지금 토큰은 아직 유효 → 이번엔 씀
                return tok["access_token"]
            raise
    return tok["access_token"]


def keep_fresh(base: Path, app_key: str, secret: str, now: float | None = None,
               post: Callable[[dict], dict] | None = None) -> list[str]:
    """매시간 감시에서 부름: 필요하면 갱신. 사람이 알아야 할 문제만 문장으로 (같은 상태면 같은 문장)"""
    now = time.time() if now is None else now
    tok = load(base)
    if not tok:
        return []                                 # 알리 토큰을 안 쓰는 상태 → 조용히
    if not (app_key and secret):
        return ["알리 토큰 갱신 불가: buyagent/.env 에 ALIEXPRESS_APP_KEY / ALIEXPRESS_APP_SECRET 없음"]
    try:
        access_token(base, app_key, secret, now, post)
    except TokenError as err:
        return [str(err)]
    tok = load(base) or tok
    exp, rexp = _ms(tok.get("expire_time")), _ms(tok.get("refresh_token_valid_time"))
    if exp and exp - now < REFRESH_BEFORE and rexp and rexp - now < WARN_BEFORE:
        return [f"알리 토큰 갱신이 안 되고 있음 — {time.strftime('%m-%d %H:%M', time.localtime(rexp))} 까지 안 되면 "
                f"{REISSUE}"]
    return []
