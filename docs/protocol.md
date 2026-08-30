# ScriptCue 通信协议规范 v1.0

服务端、主控端、被控端三方共用的通信契约。协议为 **WebSocket + JSON 文本消息**，每条消息为单行 JSON 对象，必含 `type` 字段。

## 1. 通用约定

| 项 | 约定 |
|---|---|
| 传输 | WebSocket（生产环境 WSS），端点 `/{room_code}/ws` 不适用，统一使用 `/ws` |
| 消息格式 | 单行 JSON：`{"type": "...", ...payload}` |
| 协议版本 | 客户端首条消息携带 `proto: 1`，服务端不匹配时返回 `error` 并断开 |
| 时间戳 | 除特别说明外均为 **毫秒级 Unix 时间戳**；"服务器时间"指服务端时钟，"本地时间"指发送方时钟 |
| 心跳 | 被控端每 5s 发送 `agent.heartbeat`；服务端 15s（3 次）未收到则判定离线 |
| 房间码 | 6 位字符（去除易混淆字符 0/O/1/I/L），由服务端生成 |

## 2. 连接与会话

所有角色连接同一个端点 `/ws`，**首条消息**声明角色：

### 主控端 → 服务器

```jsonc
// 创建房间（作为导控）
{"type": "controller.create", "proto": 1, "room_name": "周六下午场", "password": "可选口令"}
// 加入既有房间
{"type": "controller.join", "proto": 1, "room_code": "AB12CD", "password": "可选口令"}
// 断线重连恢复会话（凭令牌，服务器重启后令牌失效则返回错误，前端重新创建/加入）
{"type": "controller.resume", "proto": 1, "room_code": "AB12CD", "token": "..."}
```

服务器应答：

```jsonc
{"type": "controller.joined", "room_code": "AB12CD", "room_name": "...", "token": "会话令牌", "server_time": 1700000000000, "agents": [AgentState]}
{"type": "error", "code": "room_not_found|bad_password|room_full|bad_proto", "message": "..."}
```

### 被控端 → 服务器

```jsonc
// 加入房间
{"type": "agent.join", "proto": 1, "room_code": "AB12CD", "password": "可选", "nickname": "口述员-小王-剪映", "token": "重连时携带，可省略"}
// 加入时房间不存在则自动创建（用于服务器重启后客户端自动恢复房间）
{"type": "agent.join", "proto": 1, "room_code": "AB12CD", "nickname": "...", "auto_create": true, "room_name": "周六下午场"}
```

服务器应答：

```jsonc
{"type": "agent.joined", "room_code": "AB12CD", "token": "会话令牌", "server_time": ..., "compensation_ms": 0, "lead_ms": 3000}
```

### AgentState 结构（设备状态快照）

```jsonc
{
  "session_id": "agent-1",      // 服务器分配，房间内唯一
  "nickname": "口述员-小王-剪映",
  "online": true,
  "ready": false,               // 就绪标记（R-06）
  "clock_quality": "good",      // excellent|good|poor|none
  "clock_offset_ms": -42.5,     // 最近一次可信偏移估算
  "clock_rtt_ms": 23,           // 该样本的往返时延
  "compensation_ms": 0,         // 补偿值（可正可负）
  "last_seen": 1700000000000    // 最近一次收到消息的服务器时间
}
```

## 3. 授时（时钟同步）

被控端周期性发起，加入房间后立即密集采样（默认 20 次，间隔 50ms），之后每 30s 维持性采样：

```jsonc
// 被控端 → 服务器
{"type": "clock.sync_req", "id": 7, "t0": 1700000000123}   // t0 = 本地发出时刻
// 服务器 → 被控端（尽快回复，不做任何排队）
{"type": "clock.sync_res", "id": 7, "t0": 1700000000123, "ts": 1700000000156}  // ts = 服务器此刻时间
```

被控端计算：`rtt = t1 - t0`（t1 为本地收到时刻），`offset = ts - (t0 + t1) / 2`。
保留全部样本，**取 RTT 最小的样本**作为可信偏移。

时钟质量分级（被控端计算后随心跳上报，主控端展示）：

| 等级 | 条件 |
|---|---|
| excellent（优） | 样本 ≥ 10 且 最小RTT ≤ 80ms |
| good（良） | 样本 ≥ 5 且 最小RTT ≤ 200ms |
| poor（差） | 其余 |
| none（未同步） | 尚无样本 |

## 4. 指令（主控端 → 服务器 → 被控端）

### 4.1 主控端下发

```jsonc
{"type": "controller.command", "command_id": "uuid", "command": "play|pause|test", "lead_ms": 3000}
```

`lead_ms` 为提前量，默认 3000，可配置。`test` 语义同 `play`（同样绝对时刻调度），仅界面标记为测试；`test` 可附加 `"target": "agent-N"` 表示单设备测试（缺省为全体）。

### 4.2 服务器确认（→ 主控端）

```jsonc
{"type": "command.scheduled", "command_id": "uuid", "command": "play", "at": 1700000003156, "lead_ms": 3000}
```

`at` = 服务器收到请求时刻 + lead_ms，即**执行时刻 T（服务器时间基准）**。服务器随即广播：

### 4.3 服务器广播（→ 全体被控端）

```jsonc
{"type": "command.exec", "command_id": "uuid", "command": "play", "at": 1700000003156}
```

被控端换算本地触发时刻：`local_fire = at − clock_offset_ms − compensation_ms`，本地高精度定时到点执行动作（模拟空格键），执行后回执：

```jsonc
// 被控端 → 服务器
{"type": "command.receipt", "command_id": "uuid", "fired_at": 1700000003148, "status": "ok|skipped|error", "detail": "可选"}
```

`fired_at` 为实际触发时刻换算到服务器时钟（`本地触发时刻 + clock_offset_ms`）。服务器转发主控端：

```jsonc
{"type": "command.receipt", "session_id": "agent-1", "nickname": "...", "command_id": "uuid", "fired_at": ..., "delta_ms": -8, "status": "ok"}
```

`delta_ms = fired_at − at`。主控端对 `|delta_ms| > 50` 或离线未回执的设备标红。

### 4.4 取消（倒计时期间）

```jsonc
// 主控端 → 服务器
{"type": "controller.cancel", "command_id": "uuid"}
// 服务器 → 全体被控端（仅当指令尚未到期）
{"type": "command.cancel", "command_id": "uuid"}
// 服务器 → 主控端
{"type": "command.cancelled", "command_id": "uuid"}
```

被控端收到 `command.cancel` 时若该指令仍在等待队列中，立即取消本地调度。

### 4.5 补偿值远程调整

```jsonc
// 主控端 → 服务器
{"type": "controller.set_comp", "session_id": "agent-1", "compensation_ms": 80}
// 服务器 → 该被控端
{"type": "comp.update", "compensation_ms": 80}
// 服务器 → 主控端（广播新状态）
{"type": "agent.updated", "agent": AgentState}
```

## 5. 状态汇聚（被控端 → 服务器 → 主控端）

```jsonc
// 被控端 → 服务器，每 5s
{"type": "agent.heartbeat", "ready": true, "clock_quality": "excellent", "clock_offset_ms": -42.5, "clock_rtt_ms": 23, "compensation_ms": 0, "pending_command": "uuid或省略"}
// 服务器 → 主控端（状态有实质变化或定期 2s 节流推送）
{"type": "agent.updated", "agent": AgentState}
{"type": "agent.left", "session_id": "agent-1", "reason": "offline|disconnect|kicked"}
```

就绪状态变化（被控端点击"我已就绪"）同样通过 `agent.heartbeat` 上报；为降低时延，就绪切换时立即补发一次心跳。

## 6. 断线与重连

- 被控端断线后按指数退避重连（1s → 2s → 4s → 8s，上限 10s），重连成功后携带 `token` 重新 `agent.join`，并立即执行一次密集时钟采样；
- 服务器重启导致房间丢失时：被控端收到 `error: room_not_found` 后，若本端是最初的创建参与者，则以 `auto_create` 重建房间；否则轮询重试等待房间出现；
- 主控端断线重连同理；服务器重启后主控端需重新创建房间（房间码不变需导控重新告知，或凭本地保存的房间名重建）。

## 7. 错误码

| code | 含义 |
|---|---|
| `bad_proto` | 协议版本不匹配 |
| `room_not_found` | 房间不存在 |
| `bad_password` | 口令错误 |
| `room_full` | 房间设备数已达上限（20） |
| `not_controller` | 非主控端发送了指令类消息 |
| `bad_command` | 指令字段非法 |
| `no_such_agent` | 目标设备不存在 |
