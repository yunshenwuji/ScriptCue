"""ScriptCue 通信协议常量（服务端权威定义）。

与 docs/protocol.md 保持一致；被控端维护一份等价副本（agent/agent/protocol.py）。
修改协议时必须同时更新文档与两端副本，并递增 PROTO_VERSION。
"""

import os

PROTO_VERSION = 1

# ---- 消息类型 ----
# 主控端 → 服务器
CTRL_CREATE = "controller.create"
CTRL_JOIN = "controller.join"
CTRL_RESUME = "controller.resume"
CTRL_COMMAND = "controller.command"
CTRL_CANCEL = "controller.cancel"
CTRL_SET_COMP = "controller.set_comp"

# 服务器 → 主控端
CTRL_JOINED = "controller.joined"
CMD_SCHEDULED = "command.scheduled"
CMD_CANCELLED = "command.cancelled"

# 被控端 → 服务器
AGENT_JOIN = "agent.join"
AGENT_HEARTBEAT = "agent.heartbeat"
AGENT_SET_COMP = "agent.set_comp"
CLOCK_SYNC_REQ = "clock.sync_req"
CMD_RECEIPT = "command.receipt"

# 服务器 → 被控端
AGENT_JOINED = "agent.joined"
CLOCK_SYNC_RES = "clock.sync_res"
CMD_EXEC = "command.exec"
CMD_CANCEL = "command.cancel"
COMP_UPDATE = "comp.update"

# 服务器 → 主控端（状态汇聚）
AGENT_UPDATED = "agent.updated"
AGENT_LEFT = "agent.left"

ERROR = "error"

# ---- 指令类型 ----
CMD_PLAY = "play"
CMD_PAUSE = "pause"
CMD_TEST = "test"
VALID_COMMANDS = (CMD_PLAY, CMD_PAUSE, CMD_TEST)

# ---- 时钟质量分级 ----
QUALITY_EXCELLENT = "excellent"
QUALITY_GOOD = "good"
QUALITY_POOR = "poor"
QUALITY_NONE = "none"

# ---- 错误码 ----
ERR_BAD_PROTO = "bad_proto"
ERR_ROOM_NOT_FOUND = "room_not_found"
ERR_BAD_PASSWORD = "bad_password"
ERR_ROOM_FULL = "room_full"
ERR_NOT_CONTROLLER = "not_controller"
ERR_BAD_COMMAND = "bad_command"
ERR_NO_SUCH_AGENT = "no_such_agent"
ERR_BAD_MESSAGE = "bad_message"

# ---- 服务端限制 ----
MAX_AGENTS_PER_ROOM = 20          # 1 大屏 + 最多 19 台口述员
ROOM_CODE_LENGTH = 6
ROOM_CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"  # 去除 0/O/1/I/L
ROOM_IDLE_TTL_S = 24 * 3600       # 房间空闲 24h 自动销毁
# 指令提前量默认值（可配置）；在模块导入期读取，保证默认参数绑定正确
DEFAULT_LEAD_MS = int(os.environ.get("SC_DEFAULT_LEAD_MS", "3000"))

# ---- 心跳 ----
HEARTBEAT_INTERVAL_S = 5          # 被控端心跳间隔
AGENT_OFFLINE_S = 15              # 3 次心跳无响应判定离线
RECEIPT_TIMEOUT_MS = 8000         # 指令到期后等待回执的窗口
AGENT_PUSH_THROTTLE_S = 2.0       # 状态推送节流间隔
