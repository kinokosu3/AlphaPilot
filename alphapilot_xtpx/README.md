# alphapilot_xtpx

AlphaPilot 使用的 XTP Pro/XTPX 底层 Python 绑定包。

本包只包含券商 SDK 的动态库、头文件和 pybind11 封装，不注册 AlphaPilot
交易通道，也不依赖 vn.py。可发现的交易/行情通道由上层
`alphapilot-broker-xtp` 插件提供。

## 安装

从源码构建并安装：

```bash
python -m pip install ./alphapilot_xtpx
python -m pip install ./plugins/alphapilot_broker_xtp
```

发布 wheel 后可以直接安装：

```bash
python -m pip install alphapilot_xtpx alphapilot-broker-xtp
```

源码安装需要 C++17 编译器。Linux wheel 会携带 XTPX SDK 的共享库，Windows
wheel 会携带对应 DLL；发布前应确认券商 SDK 的分发许可。

## 验证

```bash
python -c "from alphapilot_xtpx.api import MdApi, TdApi; print(MdApi, TdApi)"
alphapilot live_plugins
```

应用代码不应直接用本包下单。交易与行情统一通过 AlphaPilot live gateway
协议和 `alphapilot-broker-xtp` 插件访问。
