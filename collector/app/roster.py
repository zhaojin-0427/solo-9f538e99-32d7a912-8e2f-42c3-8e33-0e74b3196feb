"""名册登记与区间核查。

- 名册按 UTC 生效小时登记,内容为自该小时起完整在运站点集合;每个生效
  小时一份名册,版本从 1 起,每次成功修改 +1。
- 登记须带所见名册版本(乐观并发控制):与库中版本不一致即拒绝,原名册
  保留。登记在 BEGIN IMMEDIATE 事务内完成"比对 + 写入",同版并发修改
  只有一份能生效。
- 区间核查实时计算、不落缓存:补登较早名册或采集修订后,受影响小时立即
  按新状态核查。整个区间在单个只读事务(WAL 一致快照)内读取,不会混用
  交错提交前后的名册与读数。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from . import db as dbmod

MAX_AUDIT_HOURS = 48


class RosterConflict(Exception):
    """所见名册版本与库中不一致:本次登记作废,原名册保留。"""

    def __init__(self, effective_hour: str, seen: int, current: int):
        self.effective_hour = effective_hour
        self.seen = seen
        self.current = current
        super().__init__(
            f"roster for {effective_hour}: seen version {seen} "
            f"does not match current version {current}; original roster kept"
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hour_plus(hour: str, n: int) -> str:
    dt = datetime.strptime(hour, "%Y-%m-%dT%H") + timedelta(hours=n)
    return dt.strftime("%Y-%m-%dT%H")


def _roster_dict(row: sqlite3.Row) -> dict:
    return {
        "effective_hour": row["effective_hour"],
        "version": row["version"],
        "stations": json.loads(row["stations"]),
        "updated_at": row["updated_at"],
    }


# ---------------- 名册登记 ----------------
def register_roster(effective_hour: str, seen_version: int, stations: list[str]) -> dict:
    """登记/修改某生效小时的名册。

    seen_version 必须等于库中当前版本(尚无名册时为 0);不一致抛
    RosterConflict,事务回滚,原名册保留。
    """
    db = dbmod.connect()
    try:
        with dbmod.tx(db):  # BEGIN IMMEDIATE:并发登记串行化,同版只有一份生效
            row = db.execute(
                "SELECT version FROM rosters WHERE effective_hour=?",
                (effective_hour,),
            ).fetchone()
            current = row["version"] if row is not None else 0
            if current != seen_version:
                raise RosterConflict(effective_hour, seen_version, current)
            version = current + 1
            payload = json.dumps(sorted(stations))
            if row is None:
                db.execute(
                    "INSERT INTO rosters(effective_hour, version, stations, updated_at) "
                    "VALUES (?,?,?,?)",
                    (effective_hour, version, payload, _now()),
                )
            else:
                db.execute(
                    "UPDATE rosters SET version=?, stations=?, updated_at=? "
                    "WHERE effective_hour=?",
                    (version, payload, _now(), effective_hour),
                )
        return {
            "effective_hour": effective_hour,
            "version": version,
            "station_count": len(stations),
        }
    finally:
        db.close()


def get_roster(effective_hour: str) -> dict | None:
    db = dbmod.connect()
    try:
        row = db.execute(
            "SELECT * FROM rosters WHERE effective_hour=?", (effective_hour,)
        ).fetchone()
        return _roster_dict(row) if row is not None else None
    finally:
        db.close()


def list_rosters() -> list[dict]:
    db = dbmod.connect()
    try:
        rows = db.execute(
            "SELECT * FROM rosters ORDER BY effective_hour"
        ).fetchall()
        return [_roster_dict(r) for r in rows]
    finally:
        db.close()


# ---------------- 区间核查 ----------------
def audit_range(start_hour: str, count: int) -> dict:
    """逐小时用当时生效的名册对照已确认的默认供数版。

    整个区间在单个只读事务里读取:WAL 下为一致快照,区间结果不会混用
    交错提交前后的名册与读数。
    """
    db = dbmod.connect()
    try:
        db.execute("BEGIN")  # 只读事务:区间内的读取看到同一快照
        try:
            rosters = db.execute(
                "SELECT effective_hour, version, stations FROM rosters "
                "ORDER BY effective_hour"
            ).fetchall()
            results = [
                _audit_hour(db, rosters, _hour_plus(start_hour, i))
                for i in range(count)
            ]
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        return {"from": start_hour, "hours": count, "results": results}
    finally:
        db.close()


def _audit_hour(db: sqlite3.Connection, rosters: list[sqlite3.Row], hour: str) -> dict:
    # 当时生效的名册:effective_hour <= hour 的最新一份(rosters 已按生效小时升序)
    roster = None
    for r in rosters:
        if r["effective_hour"] <= hour:
            roster = r
        else:
            break

    hour_row = dbmod.get_hour(db, hour)
    data_version = hour_row["current_version"] if hour_row is not None else 0

    result = {
        "hour": hour,
        "status": "ok",
        "roster_effective_hour": roster["effective_hour"] if roster else None,
        "roster_version": roster["version"] if roster else None,
        "data_version": data_version or None,
        "missing_stations": [],
        "extra_stations": [],
    }
    if roster is None:
        result["status"] = "no_roster"  # 没有当时生效的名册
        return result
    if not data_version:
        result["status"] = "not_collected"  # 尚未采集(无已确认供数版)
        return result

    roster_stations = set(json.loads(roster["stations"]))
    data_stations = set(dbmod.snapshot(db, hour, data_version))
    missing = sorted(roster_stations - data_stations)
    extra = sorted(data_stations - roster_stations)
    result["missing_stations"] = missing
    result["extra_stations"] = extra
    result["status"] = "ok" if not missing and not extra else "mismatch"
    return result
