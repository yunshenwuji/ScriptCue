"""ScriptCue 通信协议常量（被控端副本）。

服务端权威定义见 server/server/protocol.py，消息定义见 docs/protocol.md。
本文件只复制服务端的「共享契约」部分，不是逐字等价副本；被控端独有的运行参数
只存在于本文件，服务端那份没有。

共享契约（两端必须逐字一致，改动时递增 PROTO_VERSION）：
    PROTO_VERSION；消息类型；指令类型与 VALID_COMMANDS；时钟质量分级 QUALITY_*；
    错误码 ERR_*；HEARTBEAT_INTERVAL_S。

被控端独有（不在同步范围内，改动不递增 PROTO_VERSION）：
    RECONNECT_BACKOFF_S、RECONNECT_BACKOFF_MAX_S、DENSE_SYNC_SAMPLES、
    DENSE_SYNC_INTERVAL_MS、MAINTAIN_SYNC_INTERVAL_S、SPIN_THRESHOLD_MS。

跨端耦合但不要求逐字一致：
    DEFAULT_LEAD_MS——权威默认值在服务端（可由 SC_DEFAULT_LEAD_MS 覆盖），
        加入房间时经 agent.joined 下发；本文件里这一份只是本地兜底值，
        故意不读该环境变量，理由见该常量处注释。

改动共享契约的责任链见 README「开发约定」：同一笔改动里同时更新 docs/protocol.md
与两端副本、递增 PROTO_VERSION，并由提交者逐字比对确认三者一致。
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

# ---- 本地兜底值（非共享契约） ----
# 权威默认提前量在服务端（可由 SC_DEFAULT_LEAD_MS 覆盖），加入房间时经 agent.joined
# 下发并覆盖 AgentEngine.lead_ms。本常量只用于引擎构造时初始化该属性；实际触发时刻
# 完全由服务端在 command.exec 中下发的绝对时刻 at 决定，不参与本地计算，因此这里的
# 取值与服务端默认值不同并不会影响同步精度。
# 之所以不让被控端也读 SC_DEFAULT_LEAD_MS：被控端是面向口述员的 PyInstaller 单文件
# 应用，给它加一个终端用户永远不会设置的环境变量只会扩大配置面；且 README 约定服务端
# 与被控端可独立部署、互不依赖对方的代码包，共用一个服务端专属环境变量名会引入隐式耦合。
DEFAULT_LEAD_MS = 3000

# ---- 心跳 ----
# HEARTBEAT_INTERVAL_S 属共享契约，与服务端副本逐字一致；服务端的离线判定阈值
# AGENT_OFFLINE_S 取它的 3 倍，改这里必须同步改服务端副本与 docs/protocol.md。
HEARTBEAT_INTERVAL_S = 5

# ---- 被控端重连（指数退避，上限 10s） ----
RECONNECT_BACKOFF_S = (1, 2, 4, 8)
RECONNECT_BACKOFF_MAX_S = 10

# ---- 时钟同步（R-02） ----
DENSE_SYNC_SAMPLES = 20           # 加入房间后密集采样次数
DENSE_SYNC_INTERVAL_MS = 50       # 密集采样间隔
MAINTAIN_SYNC_INTERVAL_S = 30     # 维持性采样间隔
SPIN_THRESHOLD_MS = 50            # 调度末段自旋等待阈值
