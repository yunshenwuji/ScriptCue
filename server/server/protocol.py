"""ScriptCue 通信协议常量（服务端权威定义）。

消息定义见 docs/protocol.md。被控端在 agent/agent/protocol.py 只复制本文件中的
「共享契约」部分——两份文件不是逐字等价副本，各端独有的常量只存在于自己那一份。

共享契约（两端必须逐字一致，改动时递增 PROTO_VERSION）：
    PROTO_VERSION；消息类型；指令类型与 VALID_COMMANDS；时钟质量分级 QUALITY_*；
    错误码 ERR_*；HEARTBEAT_INTERVAL_S。

各端独有（不在同步范围内，改动不递增 PROTO_VERSION）：
    服务端——MAX_AGENTS_PER_ROOM、ROOM_CODE_LENGTH、ROOM_CODE_ALPHABET、
             ROOM_IDLE_TTL_S、AGENT_OFFLINE_S、RECEIPT_TIMEOUT_MS、
             AGENT_PUSH_THROTTLE_S；
    被控端——RECONNECT_BACKOFF_S、RECONNECT_BACKOFF_MAX_S、DENSE_SYNC_SAMPLES、
             DENSE_SYNC_INTERVAL_MS、MAINTAIN_SYNC_INTERVAL_S、SPIN_THRESHOLD_MS。

跨端耦合但不要求逐字一致：
    DEFAULT_LEAD_MS——以服务端为权威默认值（可由 SC_DEFAULT_LEAD_MS 覆盖），
        经 agent.joined 下发；被控端那份只是加入房间前的本地兜底值。
    AGENT_OFFLINE_S（服务端独有）——取值依赖 HEARTBEAT_INTERVAL_S 的 3 倍关系，
        改心跳间隔时必须同步调整，docs/protocol.md 通用约定中也记录了这一关系。

改动共享契约的责任链见 README「开发约定」：同一笔改动里同时更新 docs/protocol.md
与两端副本、递增 PROTO_VERSION，并由提交者逐字比对确认三者一致。
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
# 指令提前量默认值（可配置）；在模块导入期读取，保证默认参数绑定正确。
# 这里是权威默认值，随房间创建时确定并经 agent.joined 下发给被控端；
# 被控端副本中的同名常量只是加入房间前的本地兜底值，不读取该环境变量。
DEFAULT_LEAD_MS = int(os.environ.get("SC_DEFAULT_LEAD_MS", "3000"))

# ---- 心跳与离线判定 ----
# HEARTBEAT_INTERVAL_S 属共享契约，被控端副本里有一份逐字一致的；其余为服务端独有。
HEARTBEAT_INTERVAL_S = 5          # 被控端心跳间隔
AGENT_OFFLINE_S = 15              # 3 次心跳无响应判定离线（= HEARTBEAT_INTERVAL_S × 3）
RECEIPT_TIMEOUT_MS = 8000         # 指令到期后等待回执的窗口
AGENT_PUSH_THROTTLE_S = 2.0       # 状态推送节流间隔
