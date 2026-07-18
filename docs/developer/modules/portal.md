# PortalModule

## 用户能力与 CLI

提供 Portal server、重启、scheduler、timezone 和通知命令接收器共 5 个 CLI，是 React/FastAPI 的进程入口。

## 调用流程与产物

```mermaid
flowchart LR
    CLI --> Uvicorn[FastAPI/Uvicorn]
    Uvicorn --> Static[React dist]
    Uvicorn --> Engine[MainEngine]
    Uvicorn --> Scheduler[Scheduler daemon]
```

`portal` 应先应用非敏感配置、安装重启信号处理、写 runtime projection，再启动 Uvicorn；退出时清理 runtime。存在启用计划时自动启动 scheduler，但失败不能阻止 Portal 启动。

## 参数、失败与扩展测试

`portal_restart` 只作用于已记录的本机进程。reload 模式面向开发。API 和前端开发约束见 [Portal 与 API](../portal-and-api.md)。
