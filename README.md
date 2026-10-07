# 海洋观测站浮标寄存器快照网关 (Buoy Register Snapshot Gateway)

从**可能在读取途中更新**的浮标控制器取得**可追溯、单一修订版**的寄存器快照。
操作员最终只能拿到来自同一控制器修订版的完整证据；持续换版或传输异常会得到
**稳定的失败原因**，且不会留下可被查询的半成品结果。

## 核心保证

1. **分页条件读取**：控制器每页最多 64 个寄存器（一次快照 1–512 个）。
   首页不带条件，取回当前 `revision`；后续每页都携带该修订条件。
2. **整轮丢弃重试**：任何一页出现修订变化（`409 revision_mismatch`）、
   超时、缺页（short page）、地址回显不符（重复/错页）、非 u16 值，
   本轮**全部数据丢弃**，从首页重新开始。最多 3 轮。
3. **三轮仍不稳定 → 明确冲突/失败**：持久化一条不含任何值与摘要的失败记录，
   该 snapshotId 永远不可作为证据查询；重试返回同样的稳定原因。
4. **证据完整性**：返回按地址有序的 u16 数组、上游修订号，以及把全部
   16 位值按**大端**拼接后计算的**小写 SHA-256**。
5. **幂等持久化**：
   - 同一 `snapshotId` + 相同参数：并发或网关重启后的重试只会形成并
     **回放**同一份完整快照（SQLite `snapshot_id` 唯一约束 + 进程内
     inflight 合并；只持久化终态，崩溃不留僵尸行）。
   - 同一 `snapshotId` + 不同参数：`409 parameter_conflict`。
   - 任何超时/缺页/重复地址/越界值都不会被持久化。

## 运行

```bash
# 网关宿主机端口可配置（默认 8000）
GATEWAY_HOST_PORT=9090 docker compose up --build

# 一次性验证服务：单元测试 + 构建检查 + 冒烟测试，结束后自行退出
docker compose run --rm verify        # 退出码 0 = 全部通过
```

服务：

| 服务 | 说明 | 健康检查 |
|---|---|---|
| `controller` | 可控浮标模拟器（寄存器/修订/故障注入） | `GET /healthz` |
| `gateway` | 快照网关 API，SQLite 存于卷 `gateway-data` | `GET /healthz` |
| `verify` | 一次性服务，退出码汇总所有检查，`restart: "no"` | — |

## API

### 创建/回放快照

```
POST /api/snapshots
{
  "snapshotId": "cal-2026-10-06-001",
  "deviceId": "buoy-alpha",
  "startAddr": 0,
  "count": 200            // 1..512
}
```

- `201` 新建；`200` 幂等回放（同一 snapshotId 同参数）。
- `409 parameter_conflict` 同 snapshotId 参数不同。
- `409 revision_conflict` 三轮均检测到控制器换版。
- `502 upstream_error` 三轮均超时/缺页/错页/非法值。
- `400 bad_range` 控制器明确拒绝该地址范围（不重试、不持久化）。

响应体：

```json
{
  "snapshotId": "...", "deviceId": "...", "startAddr": 0, "count": 200,
  "revision": 12,
  "values": [12, 65535, 0],
  "sha256": "9f2b…(小写 hex)"
}
```

### 查询证据

```
GET /api/snapshots/{snapshotId}
```

只有**完整、单一修订版**的快照可查；失败或不存在返回 `404`。

## 模拟器控制接口（测试用）

```
POST /control/fault  {"mode": "rev_change|timeout|error500|short_page|bad_value|bad_echo|none",
                      "n": 1, "skip": 0}
POST /control/devices/{deviceId}/regenerate      # 换版
POST /control/devices/{deviceId}/set {"addr":3,"value":42}
GET  /control/state
```

`skip` 表示先放行前 k 次读取再注入故障（用于在“读到第 2 页时”触发换版/超时）。

## 项目结构

```
app/config.py            环境变量配置（控制器地址、页大小、超时、尝试轮数…）
app/controller_client.py 单页读取与严格校验（409/400/缺页/u16/地址回显）
app/snapshot_service.py  分页循环、整轮丢弃重试、大端 SHA-256、终态持久化
app/storage.py           SQLite 终态存储 + 唯一约束 + inflight 并发合并
app/main.py              FastAPI 网关
app/simulator.py         可控浮标控制器模拟器
tests/                   31 个单元/API 测试
scripts/run_verify.py    verify 一次性服务入口
```

## 关键环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `GATEWAY_HOST_PORT` | `8000` | 网关映射到宿主机的端口 |
| `CONTROLLER_URL` | `http://controller:8080` | 控制器基址 |
| `PAGE_SIZE` | `64` | 每页寄存器数（上限 64） |
| `MAX_ATTEMPTS` | `3` | 整轮读取尝试次数 |
| `CONTROLLER_TIMEOUT` | `2` | 单页读取超时秒数 |
| `DB_PATH` | `/data/snapshots.db` | SQLite 文件（卷持久化） |
