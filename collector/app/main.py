"""采集与查询 API。"""
from __future__ import annotations

import re
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from . import db as dbmod
from . import roster as roster_mod
from . import service

HOUR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}$")


def _check_hour(hour: str) -> str:
    hour = hour.strip()
    if not HOUR_RE.match(hour):
        raise HTTPException(400, "hour must be a UTC hour like '2026-11-18T10'")
    try:
        datetime.strptime(hour, "%Y-%m-%dT%H")
    except ValueError:
        raise HTTPException(400, f"invalid UTC hour: {hour}")
    return hour


def _check_stations(stations: list[str]) -> list[str]:
    """完整站点集合:非空字符串且不允许重复。"""
    out: list[str] = []
    seen: set[str] = set()
    for s in stations:
        sid = s.strip()
        if not sid:
            raise HTTPException(400, "station_id must be a non-empty string")
        if sid in seen:
            raise HTTPException(400, f"duplicate station_id: {sid}")
        seen.add(sid)
        out.append(sid)
    return out


@asynccontextmanager
async def lifespan(app: FastAPI):
    dbmod.init()
    yield


app = FastAPI(title="PM2.5 Revision Collector", lifespan=lifespan)


class CollectRequest(BaseModel):
    hour: str


@app.get("/")
def index():
    return {"service": "pm25-collector", "docs": "/docs"}


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/collections")
def create_collection(req: CollectRequest):
    """采集某 UTC 小时:先固定源站当前版,再逐版补齐到该版。"""
    hour = _check_hour(req.hour)
    try:
        return service.collect_hour(hour)
    except service.HourBusy:
        raise HTTPException(409, f"collection already running for hour {hour}")


@app.get("/collections/{hour}")
def collection_status(hour: str):
    hour = _check_hour(hour)
    db = dbmod.connect()
    try:
        row = dbmod.get_hour(db, hour)
        if row is None:
            raise HTTPException(404, f"hour {hour} never collected")
        return dict(row)
    finally:
        db.close()


@app.get("/hours")
def list_hours():
    db = dbmod.connect()
    try:
        rows = db.execute("SELECT * FROM hours ORDER BY hour").fetchall()
        return {"hours": [dict(r) for r in rows]}
    finally:
        db.close()


@app.get("/hours/{hour}/versions")
def list_versions(hour: str):
    hour = _check_hour(hour)
    db = dbmod.connect()
    try:
        row = dbmod.get_hour(db, hour)
        if row is None:
            raise HTTPException(404, f"hour {hour} never collected")
        vers = db.execute(
            "SELECT * FROM versions WHERE hour=? ORDER BY version", (hour,)
        ).fetchall()
        return {
            "hour": hour,
            "current_version": row["current_version"],
            "versions": [dict(v) for v in vers],
        }
    finally:
        db.close()


@app.get("/hours/{hour}/data")
def hour_data(
    hour: str,
    version: Optional[int] = Query(default=None, description="缺省取当前版指针"),
    station_id: Optional[str] = Query(default=None),
):
    """查询某已确认版当时的完整数据。"""
    hour = _check_hour(hour)
    db = dbmod.connect()
    try:
        row = dbmod.get_hour(db, hour)
        if row is None:
            raise HTTPException(404, f"hour {hour} never collected")
        v = version if version is not None else row["current_version"]
        if v == 0:
            raise HTTPException(404, f"hour {hour} has no confirmed version yet")
        vrow = db.execute(
            "SELECT * FROM versions WHERE hour=? AND version=?", (hour, v)
        ).fetchone()
        if vrow is None:
            raise HTTPException(404, f"version {v} of hour {hour} is not confirmed")
        if station_id is not None:
            rows = db.execute(
                "SELECT station_id, pm25 FROM entries WHERE hour=? AND version=? AND station_id=?",
                (hour, v, station_id),
            ).fetchall()
            if not rows:
                raise HTTPException(404, f"station {station_id} not present in version {v}")
        else:
            rows = db.execute(
                "SELECT station_id, pm25 FROM entries WHERE hour=? AND version=? "
                "ORDER BY station_id",
                (hour, v),
            ).fetchall()
        return {
            "hour": hour,
            "version": v,
            "is_current": v == row["current_version"],
            "station_count": len(rows),
            "stations": [{"station_id": r["station_id"], "pm25": r["pm25"]} for r in rows],
        }
    finally:
        db.close()


# ---------------- 名册登记与区间核查 ----------------
class RosterRequest(BaseModel):
    effective_hour: str = Field(description="UTC 生效小时,如 2026-11-18T10")
    seen_version: int = Field(default=0, ge=0,
                              description="所见名册版本;尚无名册时为 0")
    stations: list[str] = Field(description="自生效小时起完整在运站点集合")


@app.post("/rosters")
def register_roster(req: RosterRequest):
    """登记名册:UTC 生效小时 + 完整站点集合 + 所见名册版本。

    所见版本与库中不一致(同版并发修改)时返回 409,原名册保留。
    """
    hour = _check_hour(req.effective_hour)
    stations = _check_stations(req.stations)
    try:
        return roster_mod.register_roster(hour, req.seen_version, stations)
    except roster_mod.RosterConflict as e:
        raise HTTPException(409, str(e))


@app.get("/rosters")
def list_rosters():
    """全部已登记名册(按生效小时升序)。"""
    return {"rosters": roster_mod.list_rosters()}


@app.get("/rosters/{hour}")
def get_roster(hour: str):
    """某 UTC 生效小时的名册。"""
    hour = _check_hour(hour)
    r = roster_mod.get_roster(hour)
    if r is None:
        raise HTTPException(404, f"no roster registered for {hour}")
    return r


@app.get("/audit")
def audit_range(
    from_: str = Query(alias="from", description="起始 UTC 小时,如 2026-11-18T10"),
    hours: int = Query(default=24, ge=1, le=roster_mod.MAX_AUDIT_HOURS,
                       description="连续小时数,至多 48"),
):
    """区间核查:逐小时用当时生效的名册对照已确认的默认供数版。

    每小时返回名册版、数据版、缺站与多出站点;没有名册或尚未采集的
    小时分别以 status=no_roster / not_collected 标明。整个区间在单个
    只读事务内读取,不混用交错提交前后的名册与读数。
    """
    start = _check_hour(from_)
    return roster_mod.audit_range(start, hours)
