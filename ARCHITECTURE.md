# SiRod Inspector v2 — 项目架构与维护手册

> 本文档梳理项目的代码结构、业务逻辑、功能模块与维护注意事项。
> 部署相关另见 [DEPLOY.md](DEPLOY.md)。
> 对应代码版本：iter72（完整 git 历史 iter27→72）。

---

## 1. 项目概述

**业务领域**：光伏单晶硅棒（"方棒"）下线后的 **100% 在线 AI 视觉检测**。把"隐裂"等缺陷棒挑出来，给 MES 报警 + 串口报警灯亮，避免不良品流入切片工序。现场为盐城 / 宜宾工厂的产线（PV-B02 / PV-B03 等）。

**物理流程**：

```
棒在编码器同步运输线上移动
   │
   ├─① 经过 海康 MV-ID2023XM-08M-RBN 扫码枪 → 扫棒身条码（如 XJN...）
   │
   ├─② 进入 BV-C3110GE 线扫相机 视野（GigE，约 5-10s 扫完一根棒）
   │
   ├─③ 软件软触发 → 抓 uint16 大图（MultiFrame 100×150 拼成 1024×15000）
   │
   ├─④ AI 检测：预处理 → 分割 → 连通块筛选 → 分类 → 判定 OK / NG
   │
   └─⑤ NG → 串口报警灯亮 + MES HTTP 上报 + 飞书推送 + 缺陷图入库
```

**技术栈**：

| 层 | 技术 |
|---|---|
| 语言/GUI | Python 3.10+（开发机用 3.12）/ PyQt6 |
| 图像 | OpenCV / NumPy / Pillow |
| AI 推理 | 外部 `D:\EasyLabel_x64\DeepLearning\dnninfer.dll`（ctypes 调用，需 USB 加密狗授权） |
| 相机 | 外部 `C:\Program Files\Bluevision\BVCam\Driver\BVCam.dll`（ctypes，callback 模式） |
| 数据 | MySQL（pymysql）/ 飞书多维表格 API / MES HTTP |
| 硬件 IO | 海康扫码枪 TCP 客户端 / 串口报警灯（pyserial） |
| 依赖管理 | uv + pyproject.toml + uv.lock（开发机），现场可退回 pip |

---

## 2. 代码结构

```
SiliconRod_v2/
├── sirod_inspector/                 # 主程序包
│   ├── main.py                      # 入口A：Halcon 兜底（TCPServer 收图）
│   ├── main_camera.py               # 入口B：相机驱动（主用）★
│   ├── algorithm/                   # AI 算法层
│   │   ├── preprocess.py            #   原图预处理（削顶/缩放/旋转）
│   │   ├── inference.py             #   推理 DLL ctypes 封装（分割+分类）
│   │   ├── pipeline.py              #   端到端流水线编排
│   │   ├── judge.py                 #   两段式缺陷判定
│   │   ├── overlay.py               #   缺陷可视化叠加（mask+bbox+中文标签）
│   │   └── units.py                 #   px↔mm 换算（iter70 新增）
│   ├── camera/
│   │   └── bv_camera.py             # BV 相机 ctypes 封装（callback 模式）
│   ├── core/                        # 核心服务层
│   │   ├── inspect_engine.py        #   检测引擎（相机+Pipeline+棒号 编排）★
│   │   ├── tcp_server.py            #   HALCON 协议 TCP 服务器 + InspectData 数据类
│   │   ├── scanner_client.py        #   扫码枪 TCP 客户端（海康协议）
│   │   ├── serial_manager.py        #   串口报警灯/PLC
│   │   ├── http_client.py           #   MES HTTP 上传（含重试队列）
│   │   ├── run_bat_manager.py       #   Run.bat 子进程管理（仅 main.py 用）
│   │   └── logger.py                #   日志初始化（按天轮转）
│   ├── data/                        # 数据持久化层
│   │   ├── config.py                #   AppConfig 单例（config.json 读写）
│   │   ├── database.py              #   MySQL 读写（自动建表/迁移）
│   │   ├── feishu.py                #   飞书多维表格同步
│   │   └── shift_stats.py           #   班次统计持久化
│   ├── ui/                          # PyQt6 界面层
│   │   ├── main_window.py           #   主窗口（导航+状态栏框架）
│   │   ├── overview_page.py         #   总览（统计+实时图+MES状态）
│   │   ├── history_page.py          #   检测记录查询/导出
│   │   ├── gallery_page.py          #   NG 缺陷图库
│   │   ├── stats_page.py            #   统计报表（matplotlib）
│   │   ├── judge_page.py            #   判定参数编辑（热加载）
│   │   ├── camera_page.py           #   相机参数读写（热更新）
│   │   ├── settings_page.py         #   通用配置
│   │   ├── log_page.py              #   实时日志查看
│   │   └── styles.py                #   深色主题 QSS
│   ├── config.v2.example.json       # ★ 配置模板（config.json 本身不入库）
│   ├── gen_manual.py                # 生成 Word 部署手册（需 python-docx）
│   └── codetcp.py                   # 扫码枪调试小工具
├── models/
│   ├── Model_seg.m                  # 分割模型 ~32 MB
│   └── Model_cls.m                  # 分类模型 ~1.5 MB（iter70 更新过权重）
├── scripts/deploy/
│   ├── check_deps.py                # 部署环境自检（红绿灯）
│   ├── build_zip.py                 # 打部署 zip
│   ├── launcher.py                  # PyInstaller exe 启动器（iter67-72）
│   ├── 新版本.bat                    # 现场启动（系统 python + 写死路径）
│   └── dev_uv.bat                   # 开发机启动（uv run + 相对路径）
├── tests/                           # smoke/regress/demo 测试与诊断脚本
├── pyproject.toml + uv.lock         # uv 依赖管理
├── DEPLOY.md                        # 部署手册
└── ARCHITECTURE.md                  # 本文档
```

---

## 3. 双入口架构

项目保留**两套相互独立的数据源入口**，下游消费链（UI/DB/飞书/MES/串口）完全共享：

```
        ┌─────────── 上游数据源（二选一）───────────┐
        │                                            │
   ┌─ main.py ──────────┐         ┌─ main_camera.py ─────────┐
   │ TCPServer          │         │  InspectEngine            │
   │ (HALCON 协议)      │         │  ├─ BVCamera (ctypes)     │
   │  ↑ Run.bat 跑       │         │  │  callback 抓图          │
   │   Halcon 推图+JSON  │         │  ├─ algorithm.Pipeline    │
   │   (mvtec-halcon)   │         │  └─ ScannerClient 注入棒号 │
   └──── InspectData ───┘         └──── InspectData ──────────┘
            │                              │
            └──────────────┬───────────────┘
                           ▼
            ┌──── 共享下游消费链 ────┐
            │ UI（各 page）          │
            │ Database（MySQL）      │
            │ FeishuSync             │
            │ MesHttpClient（HTTP）  │
            │ SerialManager（报警灯）│
            └────────────────────────┘
```

- **`main.py`**（老路径，应急兜底）：spawn `Run.bat` 跑 Halcon，`TCPServer` 监听 `127.0.0.1:3000` 收 `receive_image`+`receive_tuple`，装配 `InspectData`。依赖 `mvtec-halcon`。
- **`main_camera.py`**（主路径，当前生产用）：`InspectEngine` 直接驱动相机 + 跑 Pipeline + 注入扫码棒号，产 `InspectData`。
- **关键**：两入口产出**同一个 `InspectData` dataclass**（定义在 `core/tcp_server.py`），所以下游代码零改动可切换数据源。

**单边模块**：
- 仅 main.py：`RunBatManager`、`TCPServer`
- 仅 main_camera.py：`InspectEngine`、`ScannerClient`、`BVCamera`、`CameraPage`、`JudgePage`（后两个 UI 页只在相机模式显示）

---

## 4. 端到端业务逻辑（main_camera 路径）

```
[周期触发 loop_interval_s=2s] 或 [外部事件]
   │
   ▼
InspectEngine.trigger_once()
   │
   ├─ 1) BVCamera.trigger_and_grab()  软触发 → 收 N 帧 vstack → uint16 (15000×1024)
   │
   ├─ 2) rod_id_provider()  ★ grab 之后才取棒号（对齐扫码时序）
   │       peek-then-confirm：只 peek，不在此消费
   │
   ├─ 3) Pipeline.process(frame)
   │       preprocess()    削顶+1/3缩放+90°旋转 → 1024×3072 uint8
   │       Segmenter       像素级 label_map
   │       _extract_defects 连通块筛选(outer_radius≥5, area≥100) + 合并
   │       judge_by_rules  全局阈值筛 NG 候选
   │       Classifier      逐缺陷 crop → 分类
   │       judge_per_class 按类别 5 字段规则做最终判定
   │
   ├─ 4) detection_to_inspect_data()  装配 InspectData
   │
   └─ 5) on_inspect 回调（工作线程）
           ├─ scanner.take_if(rod)  ★ 确认消费棒号
           ├─ 预渲染 marked 大图（避免 UI 线程卡）
           └─ emit 信号 → UI 线程 _handle_inspect_data
                   ├─ overview_page 更新统计/图
                   ├─ NG → 串口报警 + gallery 入库 + 状态徽章变红
                   └─ 后台 QThreadPool 任务：
                          存图（raw/marked/crop/TIF/WebImage）
                          写 MySQL
                          飞书推送
                          MES 上传（NG，失败入重试队列）
```

### InspectData 数据契约（`core/tcp_server.py`）

UI / DB / 飞书 / MES 全部消费此 dataclass：

| 字段 | 含义 |
|---|---|
| `rod_id` | 晶棒编号（扫码枪） |
| `result` | "OK" / "NG" |
| `image` | 处理后图（numpy） |
| `defect_type` | NG 类别名（如"隐裂"） |
| `defect_count` / `max_area` / `total_area` / `max_length` | 缺陷统计量 |
| `quality` | 0=OK / 1=NG |
| `ct` | 检测耗时（秒） |
| `raw_json` | 扩展字段（含缺陷明细 DefectsJSON、图片 URL 等） |

---

## 5. 各功能模块详解

### 5.1 algorithm/ — AI 算法层

| 文件 | 职责 | 关键点 |
|---|---|---|
| `preprocess.py` | uint16 原图 → 1024×3072 uint8 | 消除光源 3 相轮换的明暗行：1/3 纵向缩放(3行均值) + 削顶(1.3×暗行均值) + 90°旋转 + 缩放。所有阈值对齐 Halcon hardcode |
| `inference.py` | `Segmenter` / `Classifier` 推理 | ctypes 调 dnninfer.dll。`init_runtime()` 会 **chdir 到 DLL 目录且不可逆**，模型路径用 GBK 编码传入 |
| `pipeline.py` | `Pipeline.process()` 端到端 | 按 dtype 自动判别：uint16 跑预处理，uint8 跳过。`set_class_rules()`/`set_judge_config()` 支持热更新 |
| `judge.py` | 两段式判定（见 §6） | `JudgeConfig`（全局阈值）+ `ClassRule`（每类 5 字段规则） |
| `overlay.py` | 缺陷可视化 | mask 半透明 + bbox + 中文标签（cv2 不支持中文用 PIL）。类别配色稳定（md5 种子，跨进程一致） |
| `units.py` | px↔mm 换算（iter70） | `radius_px_to_length_mm`（长度=直径=2×半径/ppm）、`area_px_to_mm2`（÷ppm²）。**仅显示/上报用，不影响判定**。ppm≤0 返回 0 |

### 5.2 camera/ — 相机层

`bv_camera.py`：`BVCamera` 类，ctypes 调 BVCam.dll。
- **callback 模式**（不是同步 ImageReq）：SDK 内部线程每帧到达调模块级 `_module_image_callback` 入队，主线程 `trigger_and_grab` 从队列取。
- **MultiFrame**：一次软触发吐 N 帧，外部 vstack 拼大图。首帧后可强制 `multiframe_first_wait_s` 秒等编码器稳定。
- `read_all_params()` / `configure()`：供 CameraPage 热读写硬件参数。

### 5.3 core/ — 核心服务层

| 文件 | 职责 |
|---|---|
| `inspect_engine.py` | **核心编排**：相机 + Pipeline + 棒号。`start()`(开相机+载模型) / `trigger_once()` / `run_loop()` / `apply_camera_params()`(热更新相机) |
| `tcp_server.py` | HALCON 协议 TCPServer（main.py 用）+ **`InspectData` 定义** |
| `scanner_client.py` | 海康扫码枪 TCP 客户端（iter54）。发裸字符串 `b"start"` 触发，收 barcode |
| `serial_manager.py` | 串口报警灯。`send_ng()`/`send_reset()`，信号支持 HEX/ASCII 格式 |
| `http_client.py` | MES HTTP 上传。`upload_ng()` + **重试队列**（iter70：`enqueue_retry`）。详细失败日志分层（网络/HTTP/业务） |
| `run_bat_manager.py` | Run.bat 子进程树生命周期（仅 main.py） |
| `logger.py` | 日志：全量 + 错误两文件，按天轮转 |

### 5.4 data/ — 数据持久化层

| 文件 | 职责 |
|---|---|
| `config.py` | `AppConfig` 单例，`get("a.b.c")` 点路径取值。**原子写**（.tmp + os.replace） |
| `database.py` | MySQL。**自动建表 + 缺列自动 ALTER**，兼容新旧表结构（字段映射）。表名 `squarstickresult_v2` |
| `feishu.py` | 飞书多维表格追加记录 + 失败重试队列 + 自动建字段 |
| `shift_stats.py` | 班次统计持久化（默认 08:00/20:00 清零），原子写 |

### 5.5 ui/ — 界面层（PyQt6）

| 文件 | 职责 | 关键信号 |
|---|---|---|
| `main_window.py` | 框架：导航 + 状态徽章 + 设备状态灯 | `add_page` / `set_device_status` |
| `overview_page.py` | 总览：统计卡 + NIR 实时图 + MES 状态标签 | `reset_requested` / `alarm_enabled_changed` |
| `history_page.py` | 记录查询/分页/Excel 导出 + 缺陷明细双击（iter70） | — |
| `gallery_page.py` | NG 缺陷图库（卡片流，上限 500） | `_add_defect_signal` |
| `stats_page.py` | 报表（饼图/柱状图） | — |
| `judge_page.py` | 判定参数编辑 → **热加载** | `settings_saved` |
| `camera_page.py` | 相机参数读写 → **热更新硬件** | `params_saved` |
| `settings_page.py` | 通用配置 | `serial_settings_changed` 等 |
| `log_page.py` | 实时日志（节流批量刷新） | — |

跨线程安全：工作线程 → `pyqtSignal` → UI 线程，UI 从不在工作线程直接操作 widget。

---

## 6. 判定逻辑（两段式，`algorithm/judge.py`）

```
阶段1  judge_by_rules(全局几何阈值)
        max_area / sum_area / max_count / max_length 任一超限 → 进入分类
        （iter8 后改为：只要有候选缺陷就分类，避免 per-class 严阈值被吞）
   │
   ▼
阶段2  judge_per_class(每类独立 5 字段规则)
        report_ng      是否计入 NG（false 则该类永不报）
        max_area       单缺陷面积上限
        max_length     单缺陷 outer_radius 上限
        max_count      该类个数上限
        min_confidence 分类置信度下限（低于视为模型没把握，不报）
   │
   ▼
   任一缺陷触发 → result=NG, defect_type=最严重类别
```

- **默认只有"隐裂" `report_ng=true`**，其它类别（崩边/脏污/线痕/拼缝/缺口/其他/OK）记录但不报警。
- UI "参数" tab 改完保存即时生效（**热加载**，iter24+），无需重启。

---

## 7. 配置说明（config.json）

⚠️ **`config.json` 自 iter69 起不入 git**（含现场密码），从 `config.v2.example.json` 复制。各节：

| 节 | 关键字段 | 说明 |
|---|---|---|
| `scanner` | host/port/poll_interval_s | 海康扫码枪，host=现场扫码枪 IP |
| `camera` | width/height/acquisition_mode/frame_count | 现场用 MultiFrame 100×150 |
| `database` | host/password/table | **密码必须 ASCII**（否则 pymysql 崩，见 §8） |
| `http` | enabled/url/head | MES 上报。`head.DEST_SYSTEM` 区分厂区（盐城 YC01MES / 宜宾 YB01MES） |
| `scale` | pixels_per_mm | px↔mm 标定值，现场用标准件标。仅显示用 |
| `serial` | port/ng_signal/reset_signal | 报警灯 COM 口 |
| `judge` | max_*/per_class/ng_trigger_classes | 判定参数，支持热加载 |
| `line_id` | — | 产线号，按厂区改 |

调试期 `http.enabled` / `feishu.enabled` 保持 false，稳定后翻 true。

---

## 8. 注意事项 / 踩坑铁律

> 改对应区域代码前必读。这些都是真实迭代中踩过的坑。

1. **扫码枪协议是裸字符串 `start`**（iter54）：海康 MV-ID 只认配置里的"TCP 触发文本"，发 `b"start"` 即触发。**不要**加任何 `<Set,...>`/`<Exec,...>` 命令（iter45-52 试过全失败）。

2. **NG 不弹 popup**（iter27/30）：历史上 QMessageBox modal 嵌套事件循环导致 UI 卡死。现在 NG 报警走状态徽章 + 串口灯 + 图库 + log，**不要再加任何 popup**。

3. **判定参数热加载**（iter24）：靠 Python list 整体赋值的原子性。新增运行时配置模仿此模式，**不要 partial mutate**。

4. **相机必须 callback 模式**：同步 ImageReq 拿不到 MultiFrame burst。callback 必须挂模块级（防 GC）。

5. **MultiFrame 首帧后等待**：对齐 Halcon `wait_seconds(5)`，盐城 `multiframe_first_wait_s=5`，不等会偶发收不齐帧。

6. **peek-then-confirm 棒号**：grab 之后才取棒号；trigger+inspect 成功后才 `take_if` 消费。不要 grab 前消费。

7. **rod_id 路径 sanitize**：扫码可能含 Windows 非法字符，拼路径前必须替换 `<>:"/\|?*` 等。

8. **原子写文件**：config/统计/图都用 .tmp + os.replace，防崩溃半截损坏。新增写盘逻辑照此。

9. **config.json 不入库**（iter69）：每机自建，模板见 example。**数据库密码字段必须 ASCII** —— 含中文会让 pymysql 用 latin-1 编码时直接 crash（不是连不上，是启动期异常）。

10. **MES 上报排错分层**：`[MES失败]` 日志分网络层（连接失败/超时）、HTTP 层（404/重定向/非 JSON）、业务层（业务拒绝）。换厂区注意 `http.url` IP + `head.DEST_SYSTEM` + `line_id` 都要按厂改。

11. **单实例锁**：`main_camera` 用 msvcrt 锁防多开（多开撞相机/扫码枪/串口/DLL/写盘）。

12. **远端 git 历史被 force push 改写过**：`git pull` 报 diverged 且无共同祖先时别硬 merge，用 `rebase --onto`。本机已设 `pull.rebase=true`。

---

## 9. 开发环境（开发机）

- **uv** 管理依赖 + venv，Python 3.12（winget 装的 system Python，uv 复用）。
- 启动：`uv run sirod-camera`（= `python sirod_inspector/main_camera.py`），或双击 `scripts/deploy/dev_uv.bat`。
- 同步依赖：`uv sync`；可选组 `uv sync --extra docs`（python-docx）/`--extra halcon`（mvtec-halcon）。
- **开发机限制**：无 BVCam.dll → 相机引擎启动失败但 UI 仍弹出（其余组件 graceful 降级）。要看完整流程需现场硬件或 `tests/demo_ui_*.py` mock（需自备图）。

---

## 10. 部署要点

详见 [DEPLOY.md](DEPLOY.md)。核心：
- 外部依赖：BVCam SDK + EasyLabel runtime（需加密狗授权）+ MySQL + 海康扫码枪。
- 现场用 `scripts/deploy/新版本.bat`（系统 python）；先 `check_deps.bat` 自检红绿灯。
- 数据存 `D:\SiRod_v2\`（images/ImageRaw/WebImage），不碰老版 `D:\SiRod\`。
- 上线分阶段：影子运行（不发 MES/飞书）→ MES 上线 → 完全切换。
