# 数据与股票池

## 适用场景与前置条件

用于建立因子研究、Qlib 回测和策略实例共同使用的行情与 universe。需要可用的数据源、股票列表和可写的数据目录；执行 Qlib 转换前还应确认 provider 目录配置正确。删除、裁剪和覆盖股票池均会修改本地文件，应先备份或使用 dry-run。

## 使用流程

```mermaid
flowchart LR
    S[股票列表/股票池] --> D[下载原始行情]
    D --> A[复权处理]
    A --> Q[转换 Qlib]
    Q --> R[挖掘、回测、策略实例]
    D --> K[Portal K 线]
```

Portal 入口是“行情数据”页：

![行情数据页](../assets/portal/market.png)

页面可创建数据 job、查看进度、维护单股、编辑股票池并查看 K 线；CLI 示例提供相同的主要能力，适合批处理和自动化。

## 数据准备

```bash
# 完整流水线
alphapilot prepare_data pipeline \
  --start_date=2018-01-01 --adjust_mode=backward \
  --stock_csv=important_data/stock_lists/main_stock_2026_4_27.csv

# 查看本地标的
alphapilot list_stocks --adjust_mode=backward

# 重下单个标的并同步 Qlib
alphapilot refresh_stock --symbol=600000.SSE --adjust_mode=backward
```

研究特征可以使用复权数据；账户估值、目标股数和成交必须使用不复权可交易价格。策略实例检测到日期、标的或数据版本不对齐时会保持预热或拒绝决策。

## 单股维护

删除和裁剪先使用预览：

```bash
alphapilot trim_stock --symbol=600000.SSE --start_date=2020-01-01 --dry_run=True
alphapilot delete_stock --symbol=600000.SSE --dry_run=True
```

确认结果后再把 `dry_run` 设为 `False`。若开启 `resync_qlib`，操作会同步 Qlib features 和 instruments。

## 股票池

```bash
alphapilot pool_create \
  --name=demo_pool --symbols=600000.SSE,510300.SSE \
  --description="文档示例"
alphapilot pool_show --name=demo_pool
alphapilot pool_add --name=demo_pool --symbols=000001.SZ
alphapilot pool_export --name=demo_pool --output=/tmp/demo_pool.csv
alphapilot pool_delete --name=demo_pool --dry_run=True
```

股票池同时保存 JSON 和 Qlib instrument 文件，可以作为挖掘、回测和策略实例的 universe。修改正在运行的正式实例所绑定股票池，会改变配置哈希并使旧证据和授权失效；应先停止实例再创建或更新配置。

## 可视化和输出

- Portal K 线读取下载后的本地 CSV。
- `alphapilot data_viz` 是独立 Streamlit 回退工具；日常优先使用 Portal。
- 数据路径由环境配置控制，不要在插件或策略中硬编码工作目录。

输入为股票列表、日期范围、数据源和复权口径；主要产物是原始/复权 CSV、复权因子、Qlib features/calendars/instruments，以及股票池 JSON 和 instrument 文件。写操作完成后应检查标的数、最新日期和数据版本，再交给策略运行时。

## 安全提示

- `delete_stock`、`trim_stock`、`pool_delete` 和覆盖式保存是破坏性操作。
- 正式实例运行期间不要就地修改其股票池；停止后创建新配置并重新验证。
- 交易 sizing 不得读取复权价格，缺少原始报价时应保持阻断。

## 常见错误

- 标的格式不一致：数据源可能使用 `sh.600000`，交易契约使用 `600000.SSE`；通过适配器转换，不在策略中拼接。
- 复权因子缺失：重新下载原始数据和复权因子后再转换。
- 非交易日日历缺失：生产决策会 fail closed，不应使用“任意日期都是交易日”的回退。
