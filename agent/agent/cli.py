"""被控端命令行版（M1）。

用于跑通与验证"时钟同步 + 绝对时刻触发"核心链路：

    python -m agent.cli --server ws://127.0.0.1:8000 --room AB12CD --nickname 测试机

交互命令：
    ready / notready      切换就绪状态
    comp <毫秒>           本地修改补偿值（可为负）
    status                查看当前时钟同步状态
    quit                  退出

加 --dry-run 可跳过实际按键注入（联调/演练用）。
"""

import argparse
import asyncio
import logging
import sys
import threading

from .engine import AgentEngine
from .keysender import KeySender
from .logsetup import setup_logging
from .timeutil import now_ms

logger = logging.getLogger("scriptcue.agent")


def _ts() -> str:
    """本地时间 HH:MM:SS.mmm，便于与主控端/录像对时。"""
    import datetime
    return datetime.datetime.now().strftime("%H:%M:%S") + \
        f".{int(now_ms()) % 1000:03d}"


def make_event_printer():
    def on_event(evt: dict) -> None:
        event = evt.get("event")
        if event == "connecting":
            print(f"[{_ts()}] 正在连接 {evt['url']} ...")
        elif event == "connected":
            print(f"[{_ts()}] 已加入房间 {evt['room_code']}，开始密集时钟采样")
        elif event == "disconnected":
            print(f"[{_ts()}] 连接已断开")
        elif event == "reconnecting":
            print(f"[{_ts()}] {evt['delay_s']} 秒后重连...")
        elif event == "error":
            print(f"[{_ts()}] 错误: {evt.get('message')}")
        elif event == "clock_sample":
            print(f"[{_ts()}] 时钟采样 #{evt['samples']}: "
                  f"偏移={evt['offset_ms']}ms RTT={evt['rtt_ms']}ms 质量={evt['quality']}")
        elif event == "command_scheduled":
            print(f"[{_ts()}] 收到指令 [{evt['command']}] #{evt['command_id'][:8]} "
                  f"将于 {evt['remaining_ms']}ms 后触发 "
                  f"(偏移={evt['offset_ms']}ms 补偿={evt['compensation_ms']}ms)")
        elif event == "command_fired":
            print(f"[{_ts()}] 已触发 [{evt['command']}] #{evt['command_id'][:8]} "
                  f"偏差={evt['delta_ms']}ms 状态={evt['status']}")
        elif event == "command_cancelled":
            print(f"[{_ts()}] 指令已取消 #{evt['command_id'][:8]}")
        elif event == "fire_skipped":
            print(f"[{_ts()}] 触发跳过: {evt['reason']}")
        elif event == "comp_changed":
            print(f"[{_ts()}] 补偿值更新为 {evt['compensation_ms']}ms")
    return on_event


def input_loop(engine: AgentEngine, stop: threading.Event) -> None:
    """交互命令线程。"""
    while not stop.is_set():
        try:
            line = input().strip().lower()
        except EOFError:
            break
        if not line:
            continue
        if line in ("quit", "exit", "q"):
            engine.stop()
            stop.set()
            break
        elif line == "ready":
            engine.set_ready(True)
            print(f"[{_ts()}] 已标记就绪")
        elif line == "notready":
            engine.set_ready(False)
            print(f"[{_ts()}] 已取消就绪")
        elif line == "status":
            clock = engine.clock
            print(f"[{_ts()}] 连接={engine.connected} 就绪={engine.ready} "
                  f"偏移={clock.offset_ms}ms RTT={clock.rtt_ms}ms "
                  f"质量={clock.quality()} 样本数={len(clock.samples)} "
                  f"补偿={engine.compensation_ms}ms")
        elif line.startswith("comp"):
            parts = line.split()
            if len(parts) == 2:
                try:
                    engine.set_compensation(int(parts[1]))
                    print(f"[{_ts()}] 补偿值已设置为 {parts[1]}ms")
                except ValueError:
                    print("用法: comp <整数毫秒>")
            else:
                print("用法: comp <整数毫秒>")
        else:
            print("可用命令: ready / notready / comp <ms> / status / quit")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="述播 ScriptCue 被控端（命令行版）")
    parser.add_argument("--server", required=True,
                        help="服务器地址，如 ws://127.0.0.1:8000 或 https://cue.example.com")
    parser.add_argument("--room", required=True, help="6 位房间码")
    parser.add_argument("--nickname", required=True, help="设备昵称，如 口述员-小王-剪映")
    parser.add_argument("--password", default=None, help="房间口令（如有）")
    parser.add_argument("--room-name", default=None, help="房间名（服务器重启后自动重建房间时使用）")
    parser.add_argument("--dry-run", action="store_true", help="演练模式：不实际注入按键")
    args = parser.parse_args(argv)

    log_path = setup_logging()
    print(f"[{_ts()}] 日志文件: {log_path}")

    key_sender = KeySender(dry_run=args.dry_run)
    ok, message = key_sender.check()
    print(f"[{_ts()}] 按键自检: {message}")
    if ok:
        logger.info("按键自检: %s", message)
    else:
        logger.error("按键自检失败: %s", message)
        return 1

    stop = threading.Event()
    engine = AgentEngine(server_url=args.server, room_code=args.room,
                         nickname=args.nickname, password=args.password,
                         room_name=args.room_name, key_sender=key_sender,
                         on_event=make_event_printer())
    threading.Thread(target=input_loop, args=(engine, stop), daemon=True).start()

    print(f"[{_ts()}] 述播被控端启动，房间码 {args.room}，昵称 {args.nickname}")
    print(f"[{_ts()}] 命令: ready / notready / comp <ms> / status / quit")
    try:
        asyncio.run(engine.run())
    except KeyboardInterrupt:
        print(f"\n[{_ts()}] 已退出")
    logger.info("被控端退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
