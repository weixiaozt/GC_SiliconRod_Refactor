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
