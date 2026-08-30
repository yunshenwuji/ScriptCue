---
kind: external_dependency
name: websockets Python 客户端库
slug: websockets-python
category: external_dependency
category_hints:
    - vendor_identity
scope:
    - '**'
---

### websockets
- 角色：被控端（agent）的 WebSocket 客户端库，用于连接服务器的 `/ws` 端点，收发心跳、时钟同步、指令与回执。
- 集成点：`agent/requirements.txt` 声明 `websockets>=12,<16`；被控端通过该库建立长连接并实现指数退避重连。
- 使用方式：遵循协议文档 v1.0 的消息格式（JSON 文本），首条消息携带 `proto: 1`；断线后按 1s→2s→4s→8s（上限 10s）重试。
- 方向：升级时需关注与 FastAPI/websockets 服务端版本的兼容性；协议层不随库版本变化。