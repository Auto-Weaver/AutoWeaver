# IgH EtherCAT Master + 汇川 SV660N 踩坑记录

> 适用于：IgH stable-1.5 + 汇川 SV660N（EtherCAT 伺服）+ Ubuntu 24.04 RT kernel  
> 日期：2026-04

## 背景

用 Rust 编写 motion-runtime 控制汇川 SV660N 伺服驱动器。最初尝试 ethercrab（纯 Rust EtherCAT 库），最终切换到 IgH EtherCAT Master（Linux 内核模块）。

## Pitfall 1：ethercrab 无法正确配置 DC SYNC

**现象**：SV660N 卡在 SAFE-OP，报 AL status 0x0027（Invalid DC SYNC Configuration），无法进入 OP。

**根因**：SV660N 要求 DC SYNC0 时钟同步（AssignActivate = 0x0300）。ethercrab 的类型状态机 API 在 PREOP 阶段不暴露 DC 配置接口——configure_dc_sync() 只能在 SAFE-OP 之后调用，但 SV660N 要求 DC 在 PREOP→SAFE-OP 转换前就配好。

**结论**：这是 ethercrab 的架构限制，不是参数问题。ethercrab 适合不需要 DC 的简单 IO 模块，但对需要 DC SYNC 的伺服驱动器（汇川、倍福等）存在根本性障碍。

**解决**：切换到 IgH EtherCAT Master。IgH 的 `ecrt_slave_config_dc()` 可以在 activate 之前任意时机调用，DC 配置时序完全可控。

## Pitfall 2：EoE 干扰 CoE 邮箱通信

**现象**：切换到 IgH 后，SV660N 卡在 PREOP，dmesg 报：
```
EtherCAT: Unknown error reply code 0x0000
EtherCAT: No response  (SDO timeout)
EtherCAT: Invalid input configuration (0x001E)
```

**根因**：IgH 默认启用 EoE（Ethernet over EtherCAT）。EoE 和 CoE 共用邮箱通道。SV660N 的 EEPROM 标记了 `Enable PDO Configuration: yes`，IgH 在 PREOP→SAFEOP 转换时必须通过 CoE SDO 写入 0x1C12/0x1C13 来配置 PDO 映射。EoE 流量抢占邮箱导致 CoE SDO 超时，配置失败。

**解决**：重编译 IgH 时加 `--disable-eoe`：
```bash
./configure --disable-eoe --enable-generic --disable-8139too --prefix=/usr/local
make clean && make -j$(nproc) && make modules
sudo make install && sudo make modules_install
```

这是 IgH + 汇川驱动器的已知兼容性问题。

## Pitfall 3：ec_slave_info_t 结构体布局不能猜

**现象**：`ethercat slaves` 命令正常显示设备名，但 Rust FFI 读出的 `ec_slave_info_t.name` 是乱码。

**根因**：ecrt.h 中 `ec_slave_info_t` 包含一个 `ec_slave_port_desc_t ports[EC_MAX_PORTS]` 数组（4 个端口 × 20 字节 = 80 字节），在头文件里不容易看出实际大小。直接按字段顺序排列会导致 name 字段的偏移量错误。

**解决**：在目标平台上写一个 C 程序用 `offsetof()` 实测每个字段的偏移：
```c
#include <ecrt.h>
#include <stddef.h>
#include <stdio.h>
int main() {
    printf("sizeof = %zu\n", sizeof(ec_slave_info_t));
    printf("name offset = %zu\n", offsetof(ec_slave_info_t, name));
    // ...
}
```

实测结果：总大小 176 字节，name 在 offset 110。FFI 绑定中必须用 `_ports: [u8; 80]` 占位。

**教训**：FFI 结构体布局永远不要猜，必须在目标平台实测 offsetof。不同编译器、不同平台的 padding 可能不同。

## Pitfall 4：IgH 配置文件有两个路径

**现象**：重编译 IgH 后 `systemctl restart ethercat` 报 "No network cards for EtherCAT specified"，但配置文件明明写了 MAC 地址。

**根因**：IgH stable-1.5 有两套启动方式：
- init.d 脚本读 `/etc/sysconfig/ethercat`
- systemd 的 `ethercatctl` 读 `/usr/local/etc/ethercat.conf`

`make install` 会覆盖 `/usr/local/etc/ethercat.conf` 为默认空值，之前只写了 `/etc/sysconfig/ethercat` 的配置。

**解决**：安装脚本同时写两个配置文件：
```bash
mkdir -p /etc/sysconfig /usr/local/etc
echo "MASTER0_DEVICE=\"$MAC\"" > /etc/sysconfig/ethercat
echo "DEVICE_MODULES=\"generic\"" >> /etc/sysconfig/ethercat
cp /etc/sysconfig/ethercat /usr/local/etc/ethercat.conf
```

## Pitfall 5：make modules 需要单独执行

**现象**：`make -j$(nproc)` 成功但 `modprobe ec_master` 找不到模块。

**根因**：IgH 的 Makefile 中 `make` 只编译用户态库和工具，内核模块需要单独 `make modules`。

**解决**：编译步骤必须是：
```bash
make -j$(nproc)      # 用户态
make modules          # 内核模块（不能 -j，会有依赖问题）
sudo make install
sudo make modules_install
sudo depmod -a
```

## Pitfall 6：generic 驱动需要网卡先 UP

**现象**：ethercat 服务启动后所有帧超时，slave 不响应。

**根因**：IgH 的 generic 驱动不会自动激活网卡。配置文件注释里写了：
> When using the generic driver, the corresponding Ethernet device has to be activated (with OS methods, for example 'ip link set ethX up'), before the master is started.

**解决**：启动服务前确保网卡 UP：
```bash
sudo ip link set eno2 up
sudo systemctl restart ethercat
```

## Pitfall 7：PDO 映射中未写入的字段会用 0 覆盖 SDO 配置

**现象**：SV660N 进入 OP，CiA402 状态机正常走到 OperationEnabled，PP 模式 handshake 也完成了（驱动器 ACK 了 set-point），但电机完全不动。位置始终不变，target_reached 迟迟不来。

**排查过程中的弯路**：
1. 先怀疑 SDO 读回的 profile_acceleration (0x6083) 和 profile_deceleration (0x6084) 为 0 导致电机无法加速。但查手册发现 SV660N 将 0 视为 0xFFFFFFFF（最大值），所以加减速不是问题。
2. 换了一台驱动器测试，现象完全一样。
3. 尝试不同的 target_position（绝对位置 10000 vs 当前位置+1000），都不动。
4. 反复分析 handshake 时序、statusword 位变化，浪费大量时间。

**根因**：RxPDO 映射了 10 个对象，但代码只写了 4 个（controlword、target_position、profile_velocity、modes_of_operation）。其余 6 个字段在 domain buffer 中为 0，每个 1ms cycle 都会发送给驱动器。

致命的是 `0x607F max_profile_velocity`——PDO 每 cycle 写 0，覆盖了 SDO startup 配置的 500000。速度上限被锁死为 0，电机不可能动。

```
RxPDO 布局（33 字节）：
offset 0:  0x60FF target_velocity       ← 代码未写，每 cycle 发 0（PP 模式无影响）
offset 4:  0x607F max_profile_velocity  ← 代码未写，每 cycle 发 0 ← 致命！
offset 8:  0x6084 profile_deceleration  ← 代码未写，每 cycle 发 0
offset 12: 0x6083 profile_acceleration  ← 代码未写，每 cycle 发 0
offset 16: 0x6081 profile_velocity      ← 代码写了 ✓
offset 20: 0x6087 torque_slope          ← 代码未写（PP 模式无影响）
offset 24: 0x6071 target_torque         ← 代码未写（PP 模式无影响）
offset 26: 0x6060 modes_of_operation    ← 代码写了 ✓
offset 27: 0x6040 controlword           ← 代码写了 ✓
offset 29: 0x607A target_position       ← 代码写了 ✓
```

**解决**：在每个 cycle 的 tick 函数中补上缺失字段的合理值：
```rust
output_pdo[OFF_RX_MAX_PROFILE_VEL..OFF_RX_MAX_PROFILE_VEL + 4]
    .copy_from_slice(&500_000u32.to_le_bytes());   // 0x607F
output_pdo[OFF_RX_PROFILE_ACCEL..OFF_RX_PROFILE_ACCEL + 4]
    .copy_from_slice(&100_000u32.to_le_bytes());   // 0x6083
output_pdo[OFF_RX_PROFILE_DECEL..OFF_RX_PROFILE_DECEL + 4]
    .copy_from_slice(&100_000u32.to_le_bytes());   // 0x6084
```

**教训**：
- PDO 是实时通道，每个 cycle 都会覆盖驱动器内部寄存器。SDO startup 写的值在进入 OP 后会被 PDO 的 0 立即覆盖。
- PDO 映射里的每一个对象，要么代码每 cycle 写入正确值，要么就不要放进 PDO 映射。
- 遇到"命令发了但不动"的问题，第一步应该对照 PDO 映射逐字段检查实际写入值，而不是在 handshake 时序上打转。

## 最终通路

```
Rust 应用 (motion-runtime)
  → FFI (igh_ffi.rs, ~25 个函数声明)
    → libethercat.so (用户态库)
      → ec_master.ko (内核模块, --disable-eoe)
        → ec_generic.ko (generic 网卡驱动)
          → Intel I226-V NIC (eno2)
            → EtherCAT 总线
              → SV660N (PREOP → SAFEOP → OP ✓)
```

## 安装一键脚本

```bash
sudo ./scripts/install-igh-ethercat.sh eno2
```

脚本包含上述所有 pitfall 的修复。
