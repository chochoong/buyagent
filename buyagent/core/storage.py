"""SQLite 저장소: 가격 이력(재입고·품절·원가 변동) + 신상품 기록 + 알림 중복 방지"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from .models import Result


def alert_key(msg: str) -> str:
    """숫자만 다른 같은 종류의 경고는 같은 것으로 봄 (환율 때문에 금액이 조금씩 바뀌어도 재알림 안 함)"""
    return re.sub(r"[\d.,]+", "#", msg)


class Store:
    def __init__(self, path: Path):
        self.con = sqlite3.connect(path)
        self.con.executescript("""
            CREATE TABLE IF NOT EXISTS history (
                ts TEXT, pid TEXT, in_stock INTEGER, total REAL, currency TEXT,
                source TEXT, shop TEXT, url TEXT, cost_krw INTEGER);
            CREATE TABLE IF NOT EXISTS seen (
                watch TEXT, key TEXT, name TEXT, first_seen TEXT,
                PRIMARY KEY (watch, key));
            CREATE TABLE IF NOT EXISTS notified (
                pid TEXT PRIMARY KEY, keys TEXT);
        """)

    def track(self, res: Result, ts: str, change_rate: float) -> None:
        prev = self.con.execute(
            "SELECT in_stock, total, currency, cost_krw FROM history "
            "WHERE pid=? ORDER BY rowid DESC LIMIT 1", (res.spec.id,)).fetchone()
        o = res.offer
        if prev:
            was_stock, prev_total, prev_cur, prev_cost = prev
            if o is None and was_stock:
                res.events.append("🔴 재고 없음 → 마켓에서 품절 처리하세요")
            elif o is not None and not was_stock:
                res.events.append("🟢 재입고! → 판매 재개")
            elif o is not None:
                # 개당 원가(원화)로 비교: 최저가 판매처가 다른 나라·통화로 바뀌어도 정확함
                ch, detail = 0.0, ""
                if prev_cost and res.cost_krw:
                    ch = (res.cost_krw - prev_cost) / prev_cost
                    detail = f"개당 원가 {prev_cost:,} → {res.cost_krw:,}원"
                elif prev_total and prev_cur == o.currency:
                    ch = (o.total - prev_total) / prev_total
                    detail = f"{prev_total:,.0f} → {o.total:,.0f} {o.currency}"
                if detail and abs(ch) >= change_rate:
                    res.events.append(f"원가 {'▲' if ch > 0 else '▼'}{abs(ch):.1%} ({detail})")
        elif o is None:
            res.alerts.insert(0, "조건에 맞는 재고 있는 판매처 없음")
        self.con.execute("INSERT INTO history VALUES (?,?,?,?,?,?,?,?,?)", (
            ts, res.spec.id, int(o is not None), o.total if o else None,
            o.currency if o else None, o.source if o else None, o.shop if o else None,
            o.url if o else None, res.cost_krw or None))
        self.con.commit()

    def fresh_alerts(self, pid: str, msgs: list[str], replace: bool = True) -> list[str]:
        """지난 실행에 없던 경고만 돌려줌.
        replace=False(조회 실패 등)면 기존 기록을 지우지 않고 합침 → 복구 후 같은 경고가 다시 오지 않음"""
        row = self.con.execute("SELECT keys FROM notified WHERE pid=?", (pid,)).fetchone()
        prev = set(json.loads(row[0])) if row else set()
        keys = [alert_key(m) for m in msgs]
        saved = set(keys) | (set() if replace else prev)
        self.con.execute("INSERT OR REPLACE INTO notified VALUES (?,?)",
                         (pid, json.dumps(sorted(saved), ensure_ascii=False)))
        self.con.commit()
        return [m for m, k in zip(msgs, keys) if k not in prev]

    def is_first_watch(self, watch: str) -> bool:
        return self.con.execute("SELECT COUNT(*) FROM seen WHERE watch=?",
                                (watch,)).fetchone()[0] == 0

    def mark_seen(self, watch: str, key: str, name: str, ts: str) -> bool:
        """처음 보는 상품이면 True"""
        cur = self.con.execute("INSERT OR IGNORE INTO seen VALUES (?,?,?,?)",
                               (watch, key, name, ts))
        self.con.commit()
        return cur.rowcount > 0
