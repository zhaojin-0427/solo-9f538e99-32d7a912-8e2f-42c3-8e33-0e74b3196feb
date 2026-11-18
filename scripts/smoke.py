#!/usr/bin/env python3
"""端到端冒烟测试:对运行中的 source + collector 验证全部关键行为。

用法:
    python3 scripts/smoke.py                 # 默认打 http://localhost:8000 / 8001
    COLLECTOR_URL=... SOURCE_URL=... python3 scripts/smoke.py
"""
from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request

COLLECTOR = os.environ.get("COLLECTOR_URL", "http://localhost:8000").rstrip("/")
SOURCE = os.environ.get("SOURCE_URL", "http://localhost:8001").rstrip("/")
HOUR = os.environ.get("SMOKE_HOUR", "2026-11-18T10")

_checks = 0


def req(method: str, base: str, path: str, body=None, expect: int | None = 200):
    url = base + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            code, payload = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        code, payload = e.code, e.read()
    if expect is not None and code != expect:
        raise AssertionError(f"{method} {url} -> {code}, want {expect}: {payload[:400]!r}")
    return code, (json.loads(payload) if payload else {})


def check(name: str, cond: bool, extra: str = "") -> None:
    global _checks
    if not cond:
        raise AssertionError(f"CHECK FAILED: {name} {extra}")
    _checks += 1
    print(f"  ok [{_checks:02d}] {name}")


def stations(base: str, hour: str, version: int | None = None) -> dict:
    q = f"?version={version}" if version is not None else ""
    _, data = req("GET", base, f"/hours/{hour}/data{q}")
    return {s["station_id"]: s["pm25"] for s in data["stations"]}


def main() -> int:
    print(f"== smoke: collector={COLLECTOR} source={SOURCE} hour={HOUR}")

    print("-- 0. 健康检查")
    req("GET", SOURCE, "/healthz")
    req("GET", COLLECTOR, "/healthz")

    print("-- 1. 重置源站,采集首版(全量)")
    _, reset = req("POST", SOURCE, "/admin/reset", {"station_count": 20, "seed": 20261118})
    check("reset -> current=1", reset["current_version"] == 1)
    _, r = req("POST", COLLECTOR, "/collections", {"hour": HOUR})
    check("collect v1 ok", r["status"] == "ok" and r["confirmed_to"] == 1, json.dumps(r))
    v1 = stations(COLLECTOR, HOUR, 1)
    check("v1 全量 20 站", len(v1) == 20)
    check("默认查询=当前版指针", stations(COLLECTOR, HOUR) == v1)

    print("-- 2. 源站发布 v2/v3(增量),逐版补齐")
    _, p2 = req("POST", SOURCE, "/admin/publish")
    _, p3 = req("POST", SOURCE, "/admin/publish")
    deleted_v2 = [o["station_id"] for o in p2["ops"] if o["op"] == "delete"]
    _, r = req("POST", COLLECTOR, "/collections", {"hour": HOUR})
    check("补齐到 v3", r["status"] == "ok" and r["confirmed_from"] == 1 and r["confirmed_to"] == 3,
          json.dumps(r))
    v2, v3 = stations(COLLECTOR, HOUR, 2), stations(COLLECTOR, HOUR, 3)
    check("v2 删除了目标站", all(s not in v2 for s in deleted_v2) and deleted_v2,
          f"deleted={deleted_v2}")
    check("v1 仍含被删站(已确认版均可查当时数据)", all(s in v1 for s in deleted_v2))
    check("v3 含新站 N03", "N03" in v3 and "N03" not in v1)
    check("v1 数据未被后续版本污染", stations(COLLECTOR, HOUR, 1) == v1)

    print("-- 3. 重复采集:已是最新")
    _, r = req("POST", COLLECTOR, "/collections", {"hour": HOUR})
    check("up_to_date", r["status"] == "up_to_date" and r["current_version"] == 3, json.dumps(r))

    print("-- 4. 超时故障:一次性,重试后成功(重试不混残页)")
    req("PUT", SOURCE, "/admin/faults",
        {"faults": [{"version": 4, "page": 1, "kind": "timeout", "times": 1}]})
    req("POST", SOURCE, "/admin/publish")
    _, r = req("POST", COLLECTOR, "/collections", {"hour": HOUR})
    check("v4 重试后成功", r["status"] == "ok" and r["confirmed_to"] == 4, json.dumps(r))
    check("v4 第 2 次尝试才成功", r["attempts"].get("4") == 2, json.dumps(r["attempts"]))

    print("-- 5. 缺页故障:持续缺页 -> 保留原供数版")
    req("PUT", SOURCE, "/admin/faults",
        {"faults": [{"version": 5, "page": 1, "kind": "missing", "times": -1}]})
    req("POST", SOURCE, "/admin/publish")
    _, r = req("POST", COLLECTOR, "/collections", {"hour": HOUR})
    check("v5 缺页采集失败", r["status"] == "failed" and "missing page" in (r["error"] or ""),
          json.dumps(r))
    check("指针保留在 v4", r["current_version"] == 4, json.dumps(r))
    _, cur = req("GET", COLLECTOR, f"/hours/{HOUR}/data")
    check("默认查询仍是 v4(未暴露半版)", cur["version"] == 4 and cur["is_current"])
    req("GET", COLLECTOR, f"/hours/{HOUR}/data?version=5", expect=404)
    check("v5 不可查", True)
    req("DELETE", SOURCE, "/admin/faults")
    _, r = req("POST", COLLECTOR, "/collections", {"hour": HOUR})
    check("清障后补齐 v5", r["status"] == "ok" and r["confirmed_to"] == 5, json.dumps(r))

    print("-- 6. 同版同站冲突:保留原供数版")
    req("PUT", SOURCE, "/admin/faults",
        {"faults": [{"version": 6, "page": 1, "kind": "conflict", "times": -1}]})
    req("POST", SOURCE, "/admin/publish")
    _, r = req("POST", COLLECTOR, "/collections", {"hour": HOUR})
    check("v6 冲突采集失败", r["status"] == "failed" and "conflict" in (r["error"] or "").lower(),
          json.dumps(r))
    check("指针保留在 v5", r["current_version"] == 5, json.dumps(r))
    req("GET", COLLECTOR, f"/hours/{HOUR}/data?version=6", expect=404)
    check("v6 不可查", True)
    req("DELETE", SOURCE, "/admin/faults")
    _, r = req("POST", COLLECTOR, "/collections", {"hour": HOUR})
    check("清障后补齐 v6", r["status"] == "ok" and r["confirmed_to"] == 6, json.dumps(r))

    print("-- 7. 已确认版完整性:逐版快照与版本登记一致")
    _, vers = req("GET", COLLECTOR, f"/hours/{HOUR}/versions")
    check("确认版 1..6 连续", [v["version"] for v in vers["versions"]] == [1, 2, 3, 4, 5, 6])
    for v in vers["versions"]:
        snap = stations(COLLECTOR, HOUR, v["version"])
        check(f"v{v['version']} 快照站数={v['station_count']}", len(snap) == v["station_count"])

    print("-- 8. 名册登记:生效小时 + 完整站点集合 + 所见版本")
    v6 = stations(COLLECTOR, HOUR, 6)
    roster_stations = sorted(v6)
    _, a = req("GET", COLLECTOR, f"/audit?from={HOUR}&hours=1")
    check("登记前该小时 no_roster", a["results"][0]["status"] == "no_roster", json.dumps(a))
    _, r = req("POST", COLLECTOR, "/rosters",
               {"effective_hour": HOUR, "seen_version": 0, "stations": roster_stations})
    check("名册 v1 登记成功", r["version"] == 1 and r["station_count"] == len(roster_stations),
          json.dumps(r))
    req("POST", COLLECTOR, "/rosters",
        {"effective_hour": HOUR, "seen_version": 0, "stations": ["ZZZ"]}, expect=409)
    check("过期所见版本被拒(409)", True)
    _, g = req("GET", COLLECTOR, f"/rosters/{HOUR}")
    check("冲突后原名册保留", g["version"] == 1 and g["stations"] == roster_stations)
    req("POST", COLLECTOR, "/rosters",
        {"effective_hour": HOUR, "seen_version": 1,
         "stations": ["DUP", "DUP"]}, expect=400)
    check("站点集合重复被拒(400)", True)

    print("-- 9. 同版并发修改:只有一份生效,失败保留原名册")
    codes: list[int] = []

    def _reg() -> None:
        c, _ = req("POST", COLLECTOR, "/rosters",
                   {"effective_hour": HOUR, "seen_version": 1,
                    "stations": roster_stations}, expect=None)
        codes.append(c)

    threads = [threading.Thread(target=_reg) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("并发同版修改恰好一份生效", sorted(codes) == [200, 409], str(codes))
    _, g = req("GET", COLLECTOR, f"/rosters/{HOUR}")
    check("并发后名册 v2 且内容未串改", g["version"] == 2 and g["stations"] == roster_stations)

    print("-- 10. 区间核查:缺站/多出/无名册/未采集")
    dropped = roster_stations[0]
    modified = roster_stations[1:] + ["GHOST01"]
    _, r = req("POST", COLLECTOR, "/rosters",
               {"effective_hour": HOUR, "seen_version": 2, "stations": modified})
    check("名册 v3 修改成功", r["version"] == 3)
    _, a = req("GET", COLLECTOR, f"/audit?from={HOUR}&hours=1")
    res = a["results"][0]
    check("核查 mismatch", res["status"] == "mismatch", json.dumps(res))
    check("缺站=名册有而数据缺", res["missing_stations"] == ["GHOST01"], json.dumps(res))
    check("多出=数据有而名册外", res["extra_stations"] == [dropped], json.dumps(res))
    check("返回名册版与数据版", res["roster_version"] == 3 and res["data_version"] == 6,
          json.dumps(res))
    _, r = req("POST", COLLECTOR, "/rosters",
               {"effective_hour": HOUR, "seen_version": 3, "stations": roster_stations})
    check("名册 v4 改回与数据一致", r["version"] == 4)
    _, a = req("GET", COLLECTOR, "/audit?from=2026-11-18T08&hours=4")
    sts = [x["status"] for x in a["results"]]
    check("区间状态序列", sts == ["no_roster", "no_roster", "ok", "not_collected"], str(sts))
    check("未采集小时 data_version=null", a["results"][3]["data_version"] is None)
    check("未采集小时仍带名册版", a["results"][3]["roster_version"] == 4)

    print("-- 11. 补登较早名册/采集修订:受影响小时立即按新状态核查")
    H2 = "2026-11-18T12"
    _, a = req("GET", COLLECTOR, f"/audit?from={H2}&hours=1")
    check("采集前 not_collected", a["results"][0]["status"] == "not_collected")
    _, r = req("POST", COLLECTOR, "/collections", {"hour": H2})
    check("H2 补齐到 v6", r["status"] == "ok" and r["confirmed_to"] == 6, json.dumps(r))
    _, a = req("GET", COLLECTOR, f"/audit?from={H2}&hours=1")
    res = a["results"][0]
    check("采集后立即对照默认供数版", res["status"] == "ok" and res["data_version"] == 6,
          json.dumps(res))
    req("POST", SOURCE, "/admin/publish")  # v7:新增 N07
    _, r = req("POST", COLLECTOR, "/collections", {"hour": H2})
    check("H2 补齐 v7", r["confirmed_to"] == 7, json.dumps(r))
    _, a = req("GET", COLLECTOR, f"/audit?from={H2}&hours=1")
    res = a["results"][0]
    check("采集修订后核查立即用新数据版", res["data_version"] == 7, json.dumps(res))
    check("新站未入册 -> mismatch", res["status"] == "mismatch"
          and res["extra_stations"] == ["N07"], json.dumps(res))
    early = roster_stations[:-1]
    _, r = req("POST", COLLECTOR, "/rosters",
               {"effective_hour": "2026-11-18T08", "seen_version": 0, "stations": early})
    check("补登较早名册 v1", r["version"] == 1)
    _, a = req("GET", COLLECTOR, "/audit?from=2026-11-18T08&hours=3")
    res08, _, res10 = a["results"]
    check("08 立即按补登名册核查", res08["roster_effective_hour"] == "2026-11-18T08"
          and res08["roster_version"] == 1 and res08["status"] == "not_collected",
          json.dumps(res08))
    check("10 仍用 10 时生效的名册", res10["roster_effective_hour"] == HOUR
          and res10["roster_version"] == 4 and res10["status"] == "ok", json.dumps(res10))
    _, lst = req("GET", COLLECTOR, "/rosters")
    check("名册列表含两份", [x["effective_hour"] for x in lst["rosters"]]
          == ["2026-11-18T08", HOUR], json.dumps(lst))

    print("-- 12. 区间上限 48 个连续 UTC 小时")
    _, a = req("GET", COLLECTOR, f"/audit?from={HOUR}&hours=48")
    check("48 小时可核查", len(a["results"]) == 48 and a["hours"] == 48)
    req("GET", COLLECTOR, f"/audit?from={HOUR}&hours=49", expect=422)
    check("超过 48 小时被拒(422)", True)
    req("GET", COLLECTOR, "/audit?from=2026-11-18", expect=400)
    check("非法起始小时被拒(400)", True)

    print("-- 13. 源站回退保护")
    req("POST", SOURCE, "/admin/reset", {"station_count": 20, "seed": 1})
    _, r = req("POST", COLLECTOR, "/collections", {"hour": HOUR})
    check("源站回退被拒绝", r["status"] == "source_regressed" and r["current_version"] == 6,
          json.dumps(r))
    check("回退后历史版仍可查", len(stations(COLLECTOR, HOUR, 6)) > 0)

    print(f"== smoke 全部通过 ({_checks} 项检查)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
