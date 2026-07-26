# Research 子系统

`systems/research` 提供研究 campaign、选择门禁、推理一致性、执行质量和白名单等跨流程证据，不注册为运行 system。

```mermaid
flowchart LR
    Campaign --> Gates[Research gates]
    Gates --> Selection[Candidate selection]
    Selection --> Parity[Inference parity]
    Parity --> Quality[Execution quality]
    Quality --> Whitelist[Deployment whitelist]
```

该层接收已持久化研究/执行事实并计算研究资格，不直接下单或修改 Broker，也不修改 TradingSystem 的部署配置与路由权限。推理 parity 区分 PASS、MISMATCH 和 NOT_COMPARABLE；输入、代码、模型、数据、政策或前置状态不同必须判为不可比较，而不是通过。

执行质量从原始 child order/fill reconciliation 计算 implementation shortfall，不能信任调用方手工 metrics。白名单和 gates 绑定版本、环境和证据哈希。

测试覆盖 campaign 生命周期、门禁组合、状态/输出哈希、缺失证据、异常成交和配置变更失效。
