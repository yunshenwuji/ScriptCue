---
kind: external_dependency
name: Uvicorn ASGI 服务器
slug: uvicorn
category: external_dependency
category_hints:
    - vendor_identity
scope:
    - '**'
---

### Uvicorn
- 角色：FastAPI 应用的 ASGI 服务器，负责进程内事件循环与网络 I/O。
- 使用方式：生产环境通过 Dockerfile 或 docker-compose 部署，端口映射至 8000；标准模式已包含 websockets 支持。
- 方向：替换时仅影响进程模型与性能调优，不影响上层业务逻辑。