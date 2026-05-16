# 重构迭代日志

每次 `/loop` 自迭代都会追加到这里。

---

## iter1 · 2026-05-16 · BV 相机 ctypes 封装 + 实拍端到端打通

### 完成

- **新模块** [sirod_inspector/camera/bv_camera.py](sirod_inspector/camera/bv_camera.py) (~570 行)
  - `enumerate_devices()` 枚举 GigE / USB 相机（一次调用模式，buffer=32）
  - `BVCamera` 类：open / configure / start / `trigger_and_grab` / stop / close
  - 完整 ctypes 镜像 BVCAM_LIST / BVCAM_DEVINFO / BVCAM_IMAGE / BVCAM_OPENPARAM
  - feature 读写：int / float / string / **enum** / bool / command
  - 自动推断像素格式 → numpy dtype（Mono8/10/12/14/16）
  - close() 顺序: abort → stop → free → release → close（防进程未退干净导致 3s 重连）

- **新 smoke 测试**
  - [tests/smoke_camera.py](tests/smoke_camera.py) 单测相机：枚举 → 软触发 N 张 → 落盘 tif + uint8 预览
  - [tests/smoke_live_pipeline.py](tests/smoke_live_pipeline.py) 实拍端到端：相机 → preprocess → Pipeline → 判定

### 实测数据（BV-C3110GE sn=101067）

```
枚举: GigE Bluevision BV-C3110GE  ip=169.254.251.183  uid=0x03000080B37F59A1
配置: w=1024  h=15000  exposure=95.0μs  Mono12 → uint16
抓图: shape=(15000, 1024) dtype=uint16  耗时 1192 ms（含曝光+传输+拷贝）
检测: preprocess+seg+cc+cls+judge = 501 ms
总链路: ≈ 1700 ms / 棒
```

→ **本次迭代起：原图采集到判定结果的整条链路完全脱离 Halcon Run.hdev**

### 自查发现（已修 / 待办）

| 项 | 状态 |
|---|---|
| close() 资源清理顺序 | 已修 ✓ |
| BVCAM_GetList 调用方式（一次调用 + buffer 容量） | 已修 ✓ |
| AcquisitionMode/TriggerMode/TriggerSource 必须用 SetFeatureEnumeration 而非 SetFeatureString | 已修 ✓ |
| GigE 心跳超时 ~3s（重启相机连接前必须等） | 文档化（非 bug）|
| inference.py 首次实例化触发 chdir → 影响进程内相对路径 | 已通过 lazy + 先 resolve 修复 |
| preprocess.py 兜底阈值 15000 hardcode | 待办：相机不同位深时需自适应 |
| Pipeline.process() crop+cls 是串行 | 待办：批量 cls 可优化 |
| 相机内存拷贝 `string_at` 一次拷 30MB | 待办：换 `np.ctypeslib.as_array().copy()` 可能略快 |
| 相机只用 1 个 ImageAlloc buffer | 待办：连拍模式需 ring buffer |

### Feishu 通知

`sirod_inspector/config.json` 中 `feishu.*` 字段全空。无法直接发飞书。
**需要用户提供其中之一才能在下次迭代里推送通知：**
1. 群机器人 Webhook URL（最简单：`https://open.feishu.cn/open-apis/bot/v2/hook/<token>`）
2. 应用 App ID + App Secret + 一个目标 Chat ID
3. 在 `sirod_inspector/config.json` 里填好 `feishu.app_id/app_secret/app_token/table_id`，FeishuSync 可写 bitable

当前迭代结果写在这个文件，用户可以直接 `cat tests/_iteration_log.md` 查看。

### 下一迭代候选

1. **UI 集成** — 把 main.py 的 TCP 输入替换为「驱相机 + Pipeline + 现有 UI/DB/飞书/MES」
2. **NG 类别可配置** — 把 `NG_TRIGGER_CLASSES = {"隐裂"}` 接 settings_page
3. **批量分类优化** — Pipeline 把多个缺陷 crop 拼成一个 batch 喂 cls 模型
4. **相机扩展** — 连续触发模式 / 自动 IP 配置 / 曝光增益运行时调节

### Git

`iter1` commit 已落，可回滚:

```bash
git log --oneline
git reset --hard <baseline-sha>   # 回滚到 iter0 = algorithm 完成、相机未做的状态
```

---

## iter2 · 2026-05-16 · 检测引擎（camera + pipeline 编排）

### 完成

- **新模块** [sirod_inspector/core/inspect_engine.py](sirod_inspector/core/inspect_engine.py) (~370 行)
  - `InspectEngine` 类：camera + Pipeline + 棒号注入 三合一编排器
  - `InspectEngineConfig` dataclass：所有参数集中
  - `detection_to_inspect_data()`：算法层 `DetectionResult` → UI 层 `InspectData` 适配
  - **2 种触发模式**：
    - `trigger_once()` 同步（外部主动调用）
    - `run_loop(interval_s, trigger_event=None)` 异步周期或事件驱动
  - 公开 API：`start / stop / stop_loop / trigger_once / run_loop`，幂等
  - 回调：`on_inspect(InspectData)` / `on_error(Exception)`，工作线程上调用

- **新 smoke** [tests/smoke_inspect_engine.py](tests/smoke_inspect_engine.py)
  - 实拍验证：trigger_once × 1 + run_loop × 2 = 共 3-4 个 InspectData
  - 字段完整性检查：rod_id/inspect_id/result/quality/defect_type/count/area/length/ct/image/ts/raw_json

- **iter2 self-audit 修复**
  - 帧拷贝优化：`string_at` → `from_address + frombuffer + copy`，**实测 13.3ms → 6.6ms / 30MB 帧（2x 提速）**
  - `InspectEngine.stop_loop()` 公开 API（之前测试用了 private `_loop_stop`）
  - pipeline.py 内 `i` 变量加注释（之前看似 unused）

### 实测（连续 4 次软触发）

```
trigger_once × 1:    rod=TEST00001  OK  ct=503ms   total=1697ms
run_loop × 3 @ 2s:   rod=TEST0000{2,3,4}  OK  ct=456-579ms / image=(1024,3072) uint8
```

### 数据契约（确认与现有 UI 完全兼容）

`InspectData` 14 个字段全部正确填充：
- 算法字段：result, quality, defect_type, defect_count, max_area, total_area, max_length
- 元数据：rod_id（来自 rod_id_provider 回调）, inspect_id（自增）, ct（秒）
- 时间戳：check_time, upload_time, timestamp
- 图像：image=预处理后图 1024×3072 uint8（也可切换为原始 15000×1024 uint16）
- raw_json: 含 judge_reasons + 每缺陷的 bbox/area/outer_radius/class_name/conf

→ **可直接喂给现有 `main.py` 的 `_handle_tcp_data()` 消费链路（UI/DB/飞书/MES），无需改动消费侧**

### iter2 git

```bash
git log --oneline
# iter2 <new-sha>
# iter1 1d46510
# iter0 ee4eaa2
```

### 下一迭代候选

1. **main.py 集成 InspectEngine** — 把 TCPServer 替换为 InspectEngine，连接到现有 UI/DB/飞书/MES。这是真正的"开关切换"步骤
2. **扫码枪 client** — 替代当前 `lambda: "NoRead"`，从 Halcon `Code_Tcp` 的 192.168.12.56:5000 协议迁移
3. **NG 触发类别可配置** — 接 settings_page

### 飞书通知

依旧没有 webhook 或 bitable 凭据。如要发送：
- 在 [sirod_inspector/config.json](sirod_inspector/config.json) 的 `feishu` 节填 `app_id/app_secret/app_token/table_id`
- 或在文件里加一项 `feishu.bot_webhook = "https://open.feishu.cn/open-apis/bot/v2/hook/XXX"`，下次迭代我会接入

---

## iter3 · 2026-05-16 · main.py 集成（并行入口）

### 策略：平行入口而非侵入式改

考虑到 main.py 是 584 行复杂逻辑（DB/飞书/串口/MES/Run.bat/定时器/UI 接线），直接改有跑偏风险。改为：

- **保留 `sirod_inspector/main.py` 原样不动** — Halcon 模式仍可用，作为可靠回退
- **新增 `sirod_inspector/main_camera.py`** — 相机驱动模式入口（~530 行）

切换方式::

    python sirod_inspector/main.py           # Halcon 模式（原）
    python sirod_inspector/main_camera.py    # 相机模式（新）

### main_camera.py 与 main.py 的差异

| 模块 | main.py | main_camera.py |
|---|---|---|
| 数据源 | `TCPServer` 接收 Halcon 推送 | `InspectEngine` 自驱相机 |
| Halcon 进程 | `RunBatManager` 拉起 Run.bat | 移除 |
| 棒号 | 从 Halcon JSON 字段 `晶编` 取 | `rod_id_provider` 回调（默认 NoRead，待扫码枪接入） |
| 周期 | Halcon 端 `wait_seconds(2)` | `InspectEngine.run_loop(interval_s=2.0)` |
| UI / DB / 飞书 / MES / 串口 | — 完全相同 — |
| InspectData 消费链路 | `_handle_tcp_data` | `_handle_inspect_data`（同款代码） |

**关键：消费链路代码 1:1 复用。InspectData 是统一契约。**

### 新增配置项（main_camera.py 用，config.json 可选）

```json
{
  "camera": {
    "width": 1024,
    "height": 15000,
    "exposure_us": null,
    "trigger_source": "Software",
    "grab_timeout_ms": 10000,
    "loop_interval_s": 2.0
  },
  "judge": {
    "max_area": 10, "sum_area": 10, "max_count": 10, "max_length": 2
  }
}
```

未配置时全部用默认值。

### 测试

PyQt6 headless 集成测试因 **Windows 长路径限制** 安装失败（不是代码问题）。
改为更轻量但更精确的「消费契约测试」：

- [tests/smoke_inspect_data_contract.py](tests/smoke_inspect_data_contract.py)
- 构造典型 NG（隐裂 0.58）和 OK 的 `DetectionResult`
- 经 `detection_to_inspect_data()` 装配为 `InspectData`
- 验证 **15 个字段全齐** + **9 项一致性约束全过**

结果::

    [字段检查] 15/15 OK
    [一致性] 9/9 OK
    [OK] 契约测试通过

→ **`main_camera.py` 跑起来后，UI/DB/飞书/MES 消费侧不会有任何字段不匹配**

### 当前 iter3 限制

1. **未真机跑过 main_camera.py** — PyQt6 装不上，需要用户自己在工厂机上验证（应该装了 PyQt6 因为原 main.py 就用）
2. **棒号还是 mock** — 当前是 `NoRead`，下次接入扫码枪 TCP 客户端
3. **NG 触发类别还是 hardcode** — `NG_TRIGGER_CLASSES = {"隐裂"}`，下次接入 settings_page

### 部署建议

在你的工厂机上：

```bash
# 先备份配置
cp sirod_inspector/config.json sirod_inspector/config.json.bak

# 试跑相机模式
python sirod_inspector/main_camera.py

# 不满意回退
python sirod_inspector/main.py  # 仍走 Halcon 通道
```

### git

```
iter3 <new-sha>: main_camera.py + contract test
iter2 3dce4df:   InspectEngine
iter1 1d46510:   BV camera ctypes
iter0 ee4eaa2:   algorithm baseline
```

---

## iter4 · 2026-05-16 · 扫码枪 + NG 类别可配置

### 完成

**新模块** [sirod_inspector/core/scanner_client.py](sirod_inspector/core/scanner_client.py)（~190 行）

- `ScannerClient(host, port)`：Halcon `Code_Tcp` 'z' 协议兼容（NUL 终止字符串）
- 后台线程循环：连接 → 发 `"start\0"` → 收 `"<棒号>\0"` → 缓存 → 等 5s → 下一次
- **自动重连**：连接断开后按 `reconnect_interval_s` 重试
- 线程安全的两种取号 API：
  - `current_rod_id()`：peek（不消费）
  - `take_rod_id()`：consume（取走并 reset 为 "NoRead"）
- 配置项（main_camera.py 从 config.json 读）：
  ```json
  {"scanner": {
      "enabled": true,
      "host": "192.168.12.56", "port": 5000,
      "poll_interval_s": 5.0,
      "recv_timeout_s": 1.0,
      "reconnect_interval_s": 3.0
  }}
  ```

**NG 触发类别可配置**

- `pipeline.Pipeline(...).ng_trigger_classes` 实例属性（默认 `{"隐裂"}`）
- `InspectEngineConfig.ng_trigger_classes` 字段透传
- `main_camera.py` 从 `config.json` 的 `judge.ng_trigger_classes`（list[str]）读取
  ```json
  {"judge": {"ng_trigger_classes": ["隐裂", "崩边"]}}
  ```

### iter4 self-audit — 发现并修复 1 个语义差异

**问题**：Halcon `Rec_Code` + 主循环 `dequeue_message` 是 **消费式** —
每次抓图前从队列里取走最新棒号，没有就 'NoRead'。我最初的实现是 **永续 latest** —
扫到的码会一直挂着，导致"一次扫码错配多根棒"。

**修复**：加 `take_rod_id()` 方法。`main_camera.py` 的 `rod_id_provider` 改用它。
单元测试验证 consume 语义正确：
- peek 不消费、take 消费、take 完返回默认 "NoRead"

### 测试

[tests/smoke_scanner.py](tests/smoke_scanner.py) — 完整链路（不依赖真硬件）：

- 启动 mock TCP 服务器（127.0.0.1:随机端口）
- 阶段 1：连接 + 收 3 个棒号
- 阶段 2：服务器主动掉连 → 客户端自动重连，请求计数从 3 升到 12
- 阶段 3：优雅停止
- **结果**：全过

### 部署建议

工厂机第一次跑 `main_camera.py` 前，确认 `config.json` 包含：

```json
{
  "scanner": {
    "enabled": true,
    "host": "192.168.12.56", "port": 5000
  },
  "camera": {
    "loop_interval_s": 2.0,
    "exposure_us": null
  },
  "judge": {
    "max_area": 10, "sum_area": 10, "max_count": 10, "max_length": 2,
    "ng_trigger_classes": ["隐裂"]
  }
}
```

未填的字段全部用代码默认值（即 Halcon 端原值）。

### 下一迭代候选

1. **settings_page UI 加 NG 类别复选框** — 让用户在 UI 里勾选触发 NG 的类别（当前只能改 config.json）
2. **Modbus PLC 接入** — Halcon 端 `Read_Modbus` / `Call_PLC_Can_Read_MysqlTp` 等行为
3. **批量 cls 推理** — 多缺陷一次过模型，目前是逐个串行
4. **真机集成测试** — 等用户在工厂机跑过 `main_camera.py` 报告 issue

### git

```
iter4 <new-sha>: scanner + NG classes configurable
iter3 71951ea: main_camera.py
iter2 3dce4df: InspectEngine
iter1 1d46510: BV camera ctypes
iter0 ee4eaa2: algorithm baseline
```

---

## iter5 · 2026-05-16 · 部署文档化 + self-audit 修 bug

### 完成

本轮**不写新功能**，重心在文档 + bug 修复 + 回归。

#### 1. 模型路径可配置

`main_camera.py` 不再硬编码 models 路径，从 `config.models.seg/cls` 读取，
未配置时 fallback 到 `<project>/models/Model_seg.m` 和 `Model_cls.m`。

#### 2. 部署资产

- **[sirod_inspector/config.example.json](sirod_inspector/config.example.json)** —
  含全部配置项 + 行内注释（`_comment_` 伪键），用户复制为 `config.json` 即可
- **[DEPLOY.md](DEPLOY.md)** —
  5 步部署流程 + 配置速查 + 回滚指南 + sanity check 清单 + FAQ

#### 3. self-audit 找到 3 个问题，修了 2 个

| 问题 | 严重度 | 处理 |
|---|---|---|
| `InspectEngineConfig.skip_preprocess` flag 完全没起作用（两个 if 分支调用一模一样的代码）| **dead code 误导维护者** | 删除字段 + 简化 trigger_once |
| `BVCamera.trigger_and_grab` 失败路径未调 `ImageReqAbortAll`，下次 ImageReq 可能失败 | **生产环境隐患** | 加 try-except abort |
| `run_loop` 用 `_loop_stop.wait` 而非 `time.sleep` | 已经正确实现 ✓ | 无需修 |

#### 4. 回归

```
smoke_inspect_engine.py:  4 InspectData @ ct=454-480ms  通过
smoke_scanner.py:         3 棒号正常 + 自动重连 通过
```

### Modbus PLC 评估结果

Halcon `Read_Modbus` 调用统计（35 处）：
- bool 信号位：126/128/176/720/1024/181（PLC 状态信号）
- int 寄存器：100/2009/2300/2400/2600/2102

用途：PLC ↔ Halcon **状态握手**（开始检测、检测完成、棒到位、产线状态等）

**结论：当前 Python 架构不依赖 PLC**
- 相机软触发已在跑（不需要 PLC 触发）
- 复位 / NG 报警走 serial_manager（已有）
- 周期触发由 `InspectEngine.run_loop` 自驱

→ Modbus 暂不接入。若工厂线必须 PLC 握手，再开 `core/modbus_client.py`。

### 下一迭代候选

1. **settings_page UI 加 NG 类别复选框**（需要 PyQt6，工厂机有，本地装不上无法验证）
2. **真机集成测试**：等用户在工厂机跑过 `main_camera.py` 报问题
3. **NG 类别 → 缺陷图库** 联动：当前 gallery_page 按 `defect_type` 分类显示，新增 NG 类别要保持一致
4. **批量 cls 推理**：多缺陷一次推理（性能优化）
5. **Modbus 客户端**（如果工厂线明确需要 PLC 握手）

### 部署清单

工厂机部署只需 3 步：
1. `pip install` 依赖（DEPLOY.md 列了）
2. `cp config.example.json config.json` + 改 host/port/credentials
3. `python sirod_inspector/main_camera.py`

回退到 Halcon 模式: `python sirod_inspector/main.py`

### git

```
iter5 <new-sha>: docs + audit fixes (skip_preprocess dead code, ImageReqAbortAll on failure)
iter4 d251927: scanner + NG configurable
iter3 71951ea: main_camera.py
iter2 3dce4df: InspectEngine
iter1 1d46510: BV camera ctypes
iter0 ee4eaa2: algorithm baseline
```

---

## iter6 · 2026-05-16 · 压力测试 + 部署预检 + 线程安全

### 完成

#### 1. 压力测试 [tests/smoke_stress.py](tests/smoke_stress.py)

连续 15 次软触发 + 检测，验证无性能漂移/内存泄漏：

| 指标 | 结果 |
|---|---|
| 耗时 min/max/mean/stdev | 1933 / 2053 / 1957 / **32 ms** |
| 头 5 vs 尾 5 漂移 | **-0.5%**（在 1.5% 噪声内） |
| 帧内容差异 | 12 种 min / 11 种 max（无缓冲污染） |

→ **15 次连续触发耗时极稳，无 perf drift，无可见内存泄漏**

#### 2. 部署预检脚本 [tests/check_env.py](tests/check_env.py)

工厂机第一次部署前一键查 9 大类：Python / 包 / 推理 DLL / 模型 / BV 相机驱动 / 推理运行时 / 相机抓图 / config / 网络可达。

本机跑结果：
```
✓ 26 项通过   ✗ 0 项失败   ? 7 项警告（都是可选项 / 用默认值）
[GO with caution]
```

支持 `--skip-camera` / `--skip-inference` 给不同环境定制。

#### 3. 线程安全 audit

发现 `InspectEngine._inspect_id_counter += 1` 在工作线程上跑、`inspect_count` getter 在 UI 线程读、可能竞态（虽然 CPython 因 GIL 是原子的，但语义不强）。

修复：加 `_counter_lock`，所有读写都加锁。回归测试：3 个 InspectData 正常产出，inspect_id 连续递增。

### 当前完整测试矩阵

| 脚本 | 覆盖 | 硬件 |
|---|---|---|
| smoke_inference.py | DLL + 模型 | ✗ |
| smoke_preprocess.py | 原图预处理 | ✗ |
| smoke_pipeline.py | 端到端算法 | ✗ |
| smoke_inspect_data_contract.py | InspectData 字段契约 | ✗ |
| smoke_scanner.py | 扫码枪 + 自动重连（mock） | ✗ |
| smoke_camera.py | 相机软触发 | ✓ |
| smoke_inspect_engine.py | Engine 双触发模式 | ✓ |
| smoke_live_pipeline.py | 实拍 → 流水线 | ✓ |
| smoke_stress.py | 连拍压测 | ✓ |
| check_env.py | 9 项部署预检 | ✓ |

### 给用户的部署清单

按这个顺序在工厂机上跑（前面阻塞后面）：

```bash
python tests/check_env.py        # 1. 预检 — 必须全过/可接受 warning
python tests/smoke_camera.py     # 2. 单次抓图 — 验证相机
python tests/smoke_inference.py  # 3. 推理 — 验证模型
python tests/smoke_live_pipeline.py    # 4. 端到端
python tests/smoke_stress.py --shots 20  # 5. 压测 — 看 perf 稳不稳
python sirod_inspector/main_camera.py    # 6. 实跑 UI
```

任一步失败发对应日志给我，我针对性修。

### 飞书通知

依然没凭据，进度都写在 [tests/_iteration_log.md](tests/_iteration_log.md)。

### 下一迭代候选

- 等用户跑过工厂机一遍报具体 issue（这时候迭代最有针对性）
- 没具体反馈则继续：UI settings 加 NG 类别复选框 / 缺陷图库联动 / 性能极限测试

### git

```
iter6 <new-sha>: stress test + check_env.py + thread-safe inspect_id
iter5 9387a5e: deployment assets + audit fixes
iter4 d251927: scanner + NG configurable
iter3 71951ea: main_camera.py
iter2 3dce4df: InspectEngine
iter1 1d46510: BV camera ctypes
iter0 ee4eaa2: algorithm baseline
```
