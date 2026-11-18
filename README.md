# PM2.5 修订采集与查询平台

市环境监测中心会补正各站点的小时 PM2.5,分析作业需按原版复算。本平台包含:

- **source**(模拟源站,FastAPI):分页发布修订。首版(v1)含全量站点,此后每版只列
  新增、改值(`upsert`)和删除(`delete`),响应带连续版号 `version`、页号 `page`、
  总页数 `total_pages`。管理接口可发布/切换修订、注入缺页、超时、同版同站冲突故障。
- **collector**(采集与查询 API,FastAPI + SQLite):采集某 UTC 小时前先固定源站当前版,
  再逐版补齐;每个确认版的完整快照均可查询;数据与当前版指针在同一事务生效。
  另提供**名册登记**与**区间核查**:站点投运/退役后,质控员可按当时名册核查各小时
  PM2.5 数据是否到齐。

## 快速开始

```bash
docker compose up --build
```

- 采集/查询 API: http://localhost:8000 (文档 `/docs`)
- 模拟源站: http://localhost:8001 (文档 `/docs`)

端到端冒烟(59 项断言,覆盖缺页/超时/冲突/回退/名册并发/区间核查):

```bash
python3 scripts/smoke.py
```

## 典型流程

```bash
# 1. 采集某 UTC 小时(先固定源站当前版,再逐版补齐)
curl -X POST localhost:8000/collections -H 'Content-Type: application/json' \
     -d '{"hour": "2026-11-18T10"}'

# 2. 源站发布新修订(可切换修订;也可带 {"ops":[...]} 显式指定)
curl -X POST localhost:8001/admin/publish

# 3. 再次采集,逐版补齐到新固定版
curl -X POST localhost:8000/collections -H 'Content-Type: application/json' \
     -d '{"hour": "2026-11-18T10"}'

# 4. 查询:当前版(默认)或任一已确认版当时的完整数据
curl localhost:8000/hours/2026-11-18T10/data
curl 'localhost:8000/hours/2026-11-18T10/data?version=1'
curl 'localhost:8000/hours/2026-11-18T10/data?version=3&station_id=S07'
curl localhost:8000/hours/2026-11-18T10/versions
```

## 名册登记与区间核查

```bash
# 1. 登记名册:UTC 生效小时 + 完整站点集合 + 所见名册版本(首次登记 seen_version=0)
curl -X POST localhost:8000/rosters -H 'Content-Type: application/json' -d \
  '{"effective_hour":"2026-11-18T10","seen_version":0,"stations":["S01","S02","S03"]}'

# 2. 修改名册:须带当前版本;版本不符(同版并发修改)返回 409,原名册保留
curl -X POST localhost:8000/rosters -H 'Content-Type: application/json' -d \
  '{"effective_hour":"2026-11-18T10","seen_version":1,"stations":["S01","S03","S05"]}'

# 3. 区间核查:至多 48 个连续 UTC 小时
curl 'localhost:8000/audit?from=2026-11-18T08&hours=24'
```

核查逐小时返回 `roster_version`(当时生效名册版)、`data_version`(已确认默认供数版)、
`missing_stations`(名册有而数据缺)、`extra_stations`(数据有而名册外);没有名册或
尚未采集的小时分别以 `status=no_roster / not_collected` 标明。补登较早名册或采集修订
后,受影响小时立即按新状态核查;整个区间在单个只读事务内读取,不混用交错提交前后
的名册与读数。

## 故障注入演示(缺页 / 超时 / 同版同站冲突)

```bash
# 下一版第 1 页:超时 1 次(重试可成功,且不混入上次残页)
curl -X PUT localhost:8001/admin/faults -H 'Content-Type: application/json' -d \
  '{"faults":[{"version":2,"page":1,"kind":"timeout","times":1}]}'

# kind 还可取 missing(缺页 404)、conflict(同版同站不同值);times=-1 表示持续生效
curl -X DELETE localhost:8001/admin/faults   # 清除全部故障
```

采集端对每版最多重试 `MAX_ATTEMPTS` 次;仍失败则**保留原供数版**(当前版指针不动),
该小时状态记为 `failed`,清除故障后再次采集即可继续补齐。

## 需求映射

| 需求 | 实现 |
| --- | --- |
| 先固定源站当前版,再逐版补齐 | `collector/app/service.py::_collect`:开头一次 `GET /api/revisions/current` 固定目标,之后 `prev+1..pinned` 逐版抓取 |
| 缺页/超时/同版同站冲突时保留原供数版 | 错误只更新 `hours.status/last_error`,`current_version` 指针不动;失败版及后续版不确认 |
| 重试不能混入残页 | 页面只存内存、从不落库;每次重试从第 1 页整版重取(`_fetch_version`) |
| 已确认版均可查当时完整数据 | 每版确认时物化全量快照到 `entries`,`GET /hours/{hour}/data?version=N` |
| 数据与当前版指针一同生效 | 单事务写入 `versions` + `entries` + `hours.current_version`(`db.py::tx`) |
| 失败或重启不得暴露半版 | 事务原子提交;启动时仅复位 `collecting` 状态元信息;SQLite WAL + 数据卷持久化 |
| 名册登记:UTC 生效小时 + 完整站点集合 + 所见版本 | `POST /rosters`;`rosters` 表按生效小时存全量站点与名册版本(从 1 起) |
| 同版并发修改只允许一份生效,失败保留原名册 | 登记在 `BEGIN IMMEDIATE` 事务内比对 `seen_version` 后写入,不一致即 409 回滚(`roster.py::register_roster`) |
| 区间核查至多 48 个连续 UTC 小时 | `GET /audit?from=..&hours=N`,`hours` 限 1..48 |
| 逐小时按当时生效名册对照已确认默认供数版 | 每小时取 `effective_hour<=hour` 的最新名册 × `hours.current_version` 快照(`roster.py::_audit_hour`) |
| 返回名册版/数据版/缺站/多出站点 | 结果字段 `roster_version`/`data_version`/`missing_stations`/`extra_stations` |
| 没有名册或尚未采集的小时分别标明 | `status` = `no_roster` / `not_collected`(到齐为 `ok`,不齐为 `mismatch`) |
| 补登较早名册或采集修订后立即按新状态核查 | 核查实时计算,不落缓存 |
| 区间结果不混用交错提交前后的名册与读数 | 整个区间在单个只读事务(WAL 一致快照)内读取(`roster.py::audit_range`) |
| 原按版查询照旧 | 既有端点未改动 |

## API 一览

**collector(8000)**

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/collections` | 采集某 UTC 小时,body `{"hour":"YYYY-MM-DDTHH"}`;采集中返回 409 |
| GET | `/collections/{hour}` | 该小时采集状态/最近错误 |
| GET | `/hours` | 全部小时及当前版指针 |
| GET | `/hours/{hour}/versions` | 已确认版列表 |
| GET | `/hours/{hour}/data` | 某版完整数据;`version` 缺省取当前版,`station_id` 可单站过滤 |
| POST | `/rosters` | 登记名册:`{"effective_hour","seen_version","stations"}`;版本不符 409,原名册保留 |
| GET | `/rosters` | 全部已登记名册(按生效小时升序) |
| GET | `/rosters/{hour}` | 某 UTC 生效小时的名册 |
| GET | `/audit` | 区间核查:`from` 起始 UTC 小时,`hours` 连续小时数(1..48,缺省 24) |

**source(8001)**

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/revisions/current` | 当前版号 |
| GET | `/api/revisions` | 全部修订元信息 |
| GET | `/api/revisions/{v}/pages/{p}` | 某版某页(`version`/`page`/`total_pages`/`items`) |
| POST | `/admin/publish` | 发布下一修订(缺省自动生成确定性增量,可传 `ops`) |
| POST | `/admin/reset` | 重置回首版(可指定站点数与种子) |
| GET/PUT/DELETE | `/admin/faults` | 故障注入: `missing`/`timeout`/`conflict`,`times` 控制触发次数 |
| GET | `/admin/state` | 源站内部状态 |

## 本地开发(不用 Docker)

```bash
pip install -r source/requirements.txt -r collector/requirements.txt
(cd source && uvicorn app.main:app --port 8001) &
(cd collector && SOURCE_BASE_URL=http://localhost:8001 DB_PATH=/tmp/collector.db \
    uvicorn app.main:app --port 8000) &
python3 scripts/smoke.py
```
