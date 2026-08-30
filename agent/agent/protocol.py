"""ScriptCue 通信协议常量（被控端副本）。

服务端权威定义见 server/server/protocol.py，文档见 docs/protocol.md。
两份副本必须保持一致；修改协议时同时更新并递增 PROTO_VERSION。
"""

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

# ---- 限制（与服务端一致） ----
DEFAULT_LEAD_MS = 3000

# ---- 心跳 ----
HEARTBEAT_INTERVAL_S = 5

# ---- 被控端重连（指数退避，上限 10s） ----
RECONNECT_BACKOFF_S = (1, 2, 4, 8)
RECONNECT_BACKOFF_MAX_S = 10

# ---- 时钟同步（R-02） ----
DENSE_SYNC_SAMPLES = 20           # 加入房间后密集采样次数
DENSE_SYNC_INTERVAL_MS = 50       # 密集采样间隔
MAINTAIN_SYNC_INTERVAL_S = 30     # 维持性采样间隔
SPIN_THRESHOLD_MS = 50            # 调度末段自旋等待阈值
