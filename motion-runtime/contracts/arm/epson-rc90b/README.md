# Epson RC90-B EtherCAT 选件板

控制 Epson LS6-B602C SCARA 机器人。

## 文件

- `contract.yaml` — 主站（motion-runtime）加载的字段契约
- `controller_program.spel` — 烧到 RC90-B 控制器上跑的参数解释器源码

motion-runtime **只读 `contract.yaml`**，不读 `.spel` 文件——SPEL+ 代码是要烧进 RC90-B 控制器的，放在这里是为了：

1. 让人在仓库里看到当前控制器跑的是哪段逻辑
2. 改字段布局时方便对齐两边
3. 部署 / 更新机器人程序时直接从这里拷过去到 Epson 开发环境烧录

## 字节契约

`contract.yaml` 和 `controller_program.spel` 头部的字节布局表是**同一份契约**，必须严格对齐。改一边必须同时改另一边，否则 PDO 链路依然通但语义错位——这是最难排查的一类 bug。

`protocol_version` 字段用于启动期校验：将来 BT leaf 可以在第一次连上时 `read_field("arm", "protocol_version")` 对比预期值。

## 真机验证 checklist

本契约是 0.7.0 第一阶段的 stub，**数值未经真机验证**。接真机时需要确认：

- [ ] `slave_match.name_contains` 实际匹配 `ethercat slaves` 输出的设备名
- [ ] `pdo_mapping.rx_pdo_index` / `tx_pdo_index` 和 `ethercat pdos -p N` 显示的 PDO assembly index 一致
- [ ] `rx_pdo_size` / `tx_pdo_size` 不超过选件板暴露的用户数据区容量
- [ ] SPEL+ 这边的 `EcReadReal` / `EcWriteByte` 等 API 替换成真实 Epson SPEL+ 函数（参考 Epson EtherCAT 选件板编程手册）
- [ ] 数据区字节序确认（Epson 默认 little-endian？需要查文档或实测）
- [ ] 通过一个最简的运动指令（如 `routine=4` Home）确认整个回路打通

## 扩展约定

增加一种动作（例如点动 / 路径插补 / 自定义工艺）：

1. 在 `contract.yaml` 的 `routine` 字段注释里加一行新 case 的说明
2. 在 `controller_program.spel` 的 `Select Case R` 里加新 case
3. 如需新参数，在 RxPDO 留的 Spare 区扩字段（同时改两边）

**常规扩展不动 motion-runtime 代码、不动 BT leaf**（leaf 只新增 `write_field("arm", "<new_field>", ...)` 调用）。

## 参考

- 设计原则：[docs/evo/003-motion-runtime.md](../../../../docs/evo/003-motion-runtime.md)
- 单总线方案研究：[docs/research/ethercat-unified-bus-ls6-rc90b.md](../../../../docs/research/ethercat-unified-bus-ls6-rc90b.md)
