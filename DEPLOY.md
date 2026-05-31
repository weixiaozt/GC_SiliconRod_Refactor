# SiRod Inspector — 部署手册

> **适用版本**：相机驱动模式 (`main_camera.py`)，Python 重构版，**完全脱离 Halcon**。
> 老版本（Halcon + Run.hdev）仍可保留作为回退兜底，两套互不影响。

---

## 0. 一图看懂部署后目录

```
D:\
├── SiliconRod_v2\                  ← 程序主目录（本次部署）
│   ├── sirod_inspector\
│   │   ├── main_camera.py          ← 启动入口
│   │   ├── config.json             ← 现场配置（从 config.v2.example.json 拷贝并改）
│   │   ├── core/                   ← 检测引擎 / 扫码枪客户端 / 数据源
│   │   ├── camera/                 ← BV 相机封装
│   │   ├── algorithm/              ← AI 推理流水线
│   │   ├── ui/                     ← PyQt 界面
│   │   ├── data/                   ← 数据库 / 飞书 / 配置
│   │   └── logs/                   ← 运行日志（自动生成，含 main_camera.lock 单实例锁）
│   ├── models/
│   │   ├── Model_seg.m             ← 分割模型（~32 MB）
│   │   └── Model_cls.m             ← 分类模型（~1.5 MB）
│   ├── scripts/deploy/
│   │   ├── check_deps.bat          ← 一键自检（红绿灯）
│   │   ├── 新版本.bat               ← 桌面启动脚本
│   │   └── 老版本.bat.template      ← 老版本回退脚本模板
│   └── DEPLOY.md                   ← 本文档
│
└── SiRod_v2\                       ← 程序产出（首次启动自建）
    ├── images\<date>\full\          ← 全图（raw + marked）
    ├── images\<date>\crops\         ← NG 缺陷小图
    ├── ImageRaw\                    ← uint16 TIF 原图
    └── WebImage\                    ← MES 拉图用 PNG

C:\Program Files\Bluevision\BVCam\   ← BV 相机 SDK（独立装）
D:\EasyLabel_x64\DeepLearning\        ← AI 推理 DLL（独立装，需授权）
```

---

## 1. 先决条件（一台全新机器要装的东西）

| 必装项 | 安装方法 | 检查点 |
|---|---|---|
| Python **3.10+** | 官网 / 公司镜像 | `python --version` |
| Windows 长路径开启 | `regedit` → `HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem!LongPathsEnabled=1`，**重启生效** | 注册表 |
| **BV 相机 SDK**（BVCam） | DO3THINK 客户端，默认装 `C:\Program Files\Bluevision\BVCam\` | `BVCam.dll` 存在 |
| **EasyLabel runtime** | 部署 zip 里带的 `EasyLabel_DL_runtime.zip`，**直接解压到 `D:\` 根目录**（产出 `D:\EasyLabel_x64\`） | `D:\EasyLabel_x64\DeepLearning\dnninfer.dll` |
| **EasyLabel 授权** | USB 加密狗（最常见）或 license 文件，问 MVIT 供应商 | 程序启动加载 DLL 不报错 |
| **MySQL** | 现场已有 | navicat 能连 `b_xmartsql` |
| **海康扫码枪** | 看下面"扫码枪配置"section | 配置软件能连上 |

---

## 2. 部署步骤（按顺序，一步都不能跳）

### 2.1 解压部署 zip

```
SiliconRod_v2_deploy.zip → D:\SiliconRod_v2\
```

应该看到 `D:\SiliconRod_v2\sirod_inspector\main_camera.py`。

### 2.2 装 EasyLabel runtime（如果机器没装过）

部署 zip 解压后根目录有 `EasyLabel_DL_runtime.zip`（~124 MB），**直接用 Windows 资源管理器右键解压到 `D:\` 根目录**。解压完应该看到：

```
D:\EasyLabel_x64\DeepLearning\
    ├── dnninfer.dll          ← 主推理 DLL
    ├── dnndefine.dll
    ├── dnninfercpudll.dll    ← CPU 版（盐城用这个）
    ├── dnninfergpudll.dll    ← GPU 版（备用）
    └── ... 其他依赖 DLL
```

> ⚠ **不要去跑 `MvitSDK_4.1.23.622.exe` 安装器** —— 之前实测装不成功（DLL 没释放出来 / 注册表写半截）。直接解压 zip 是已经验证 work 的方式。
>
> 路径写死在 [inference.py:58](sirod_inspector/algorithm/inference.py:58)，改路径要同步改代码。

**授权关键**：DLL 自带 license 校验，必须插 USB 加密狗或有 license 文件。验证方法：跑 `python sirod_inspector/main_camera.py`，看到 `推理运行时初始化完成` + `模型已加载` 就说明 DLL + 授权都 OK。如果 `DnnInfer_Init returns NULL` 就是授权没到位。

### 2.3 装 Python 依赖

```bat
:: 一次性配清华源（永久生效，免每次加 -i）
python -m pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple

:: 装所有必需包
pip install PyQt6 opencv-python numpy psutil pymysql requests pyserial Pillow matplotlib openpyxl cryptography
```

如果还要走 Halcon 兜底（保留老版本），额外装：

```bat
pip install mvtec-halcon
```

### 2.4 初始化 config.json

```bat
cd D:\SiliconRod_v2\sirod_inspector
copy config.v2.example.json config.json
notepad config.json
```

**至少要改的 5 处**：

| 字段 | 改成什么 |
|---|---|
| `database.host` / `password` | 现场 MySQL 实际 IP / 密码 |
| `scanner.host` | 现场扫码枪 IP（看 2.5） |
| `serial.port` | 报警灯 COM 口（`COM3` / `COM4`...） |
| `image_store.web_url_base` | MES 拉图的 HTTP 根 URL，例如 `http://10.32.50.220:8080/v2` |
| `line_id` | 产线号 `PV-B02` / `PV-B03`... |

**两个 enabled 调试期保持 false**：
- `http.enabled = false`（别往 MES 推双份）
- `feishu.enabled = false`（别往飞书机器人推双份）

跑稳 3-7 天再翻 true。

### 2.5 配置海康扫码枪 ★关键★

扫码枪型号：**海康 MV-ID2023XM-08M-RBN** 或同系列。打开海康配置软件 (IDMVS / MVS)，按以下设置：

| 路径 | 设置项 | 值 |
|---|---|---|
| I/O → 输入 → 触发设置 | 触发模式 | **开启** |
| I/O → 输入 → 触发设置 | 触发源 | **TCP服务端** |
| I/O → 输入 → TCP服务器触发 | TCP服务器触发端口 | **5000** |
| I/O → 输入 → TCP服务器触发 | TCP服务端触发文本格式 | **字符串** |
| I/O → 输入 → TCP服务器触发 | TCP服务端触发文本 | **`start`**（小写，无引号） |
| I/O → 输入 → TCP服务器触发 | TCP服务器返回数据到触发端口 | **打开** |
| I/O → 输入 → 触发设置 | 命令持续触发 | 打开（推荐） |

**协议工作方式**（理解清楚少走弯路）：
1. 我们连扫码枪的 5000 端口（作为 TCP 客户端）
2. 我们发字面字符串 `b"start"` → 扫码枪开始扫码
3. 扫到码 → 扫码枪从同一个 5000 端口推 barcode 给我们
4. 我们每 5 秒（`scanner.poll_interval_s`）重发一次 `b"start"` 当 keepalive

> 扫码枪同一时刻**只允许一个 TCP 客户端连接**。跑新版本前必须先停掉老版本 Halcon（也是连这台扫码枪），否则会抢端口。

### 2.6 一键自检

```bat
D:\SiliconRod_v2\scripts\deploy\check_deps.bat
```

打印 7 项红绿灯：
1. Python 版本 >= 3.10
2. Windows 长路径开关
3. BVCam 驱动
4. EasyLabel AI DLL
5. Python 第三方包（缺哪个给一键补装命令）
6. 模型文件
7. config.json

**全绿（或仅黄色警告）才能启动主程序**。出现红色 `[X]` 必须先修。

### 2.7 启动主程序

```bat
D:\SiliconRod_v2\scripts\deploy\新版本.bat
```

或者直接命令行：

```bat
cd D:\SiliconRod_v2
python sirod_inspector\main_camera.py
```

启动按顺序应看到：
1. 控制台 "SiRod Inspector (Camera Mode) 启动中..."
2. 数据库连接成功
3. **扫码枪客户端启动 (iter53 海康 TCP 触发协议 trigger_text='start')**
4. 扫码枪已连接
5. **★ iter53 ★ init 触发 'start' 已发送**
6. BV 相机打开 `BV-C3110GE sn=XXX`
7. AI 模型加载（~20 秒）
8. UI 主窗口弹出
9. 状态徽章 "运行中" 绿色

来一根真品棒：
- log 出现 `扫码: XJN...`
- log 出现 `检测完成: rod_id=XJN..., result=OK/NG, ...`
- UI 总览数 +1
- DB `squarstickresult_v2` +1 行
- `D:\SiRod_v2\images\<date>\` 出现图

### 2.8 部署桌面启动图标

把 `D:\SiliconRod_v2\scripts\deploy\新版本.bat` **拷一份到桌面**。操作员日常双击即可。

如果保留老 Halcon 版本，编辑 `老版本.bat.template`，把 "改成老版本实际路径" 换成老版本目录（例如 `D:\silicon-rod-defect-review`），另存为 `老版本.bat` 到桌面。

桌面应该两个图标：
- 🟢 `老版本.bat` — 兜底，应急时切回去
- 🆕 `新版本.bat` — 主用

---

## 3. 日常运维

### 3.1 切换版本（操作员）

**切到新版本**：
1. 任务管理器 → 杀掉所有 `python.exe`
2. 等 5 秒（让相机/串口/扫码枪资源释放干净）
3. 双击桌面 🆕 `新版本.bat`

**回退老版本**：
1. 任务管理器 → 杀掉所有 `python.exe`
2. 等 5 秒
3. 双击桌面 🟢 `老版本.bat`

> **绝对不要同时跑两套**——会抢相机 / 扫码枪 / 串口 / 双写存图 / 双发 MES。

### 3.2 改配置（开发 / 现场调参）

`config.json` 里的判定参数 (`judge.per_class`) 支持**热加载**（iter24+）：UI 的"参数"tab 改完点保存即时生效，不用重启。

其他参数（数据库 / scanner host / camera 配置等）改完需要重启 main_camera。

### 3.3 看日志

```
D:\SiliconRod_v2\sirod_inspector\logs\sirod_inspector.log  ← 所有 INFO
D:\SiliconRod_v2\sirod_inspector\logs\sirod_error.log      ← 仅 ERROR
```

关键关键字：
- `扫码: ` — 收到 barcode
- `检测完成` — 一次完整 inspection
- `[heartbeat]` — scanner 心跳，看 `barcodes=N` 累计扫码数
- `[WORKER_HB]` — inspection worker 心跳
- `[UI_HB]` — UI 主线程心跳（卡死时停止）

实时跟踪：

```powershell
Get-Content sirod_inspector\logs\sirod_inspector.log -Wait -Tail 0 | Select-String 'heartbeat|扫码|检测完成|ERROR'
```

### 3.4 上线 → 稳定 → 切量

| 阶段 | 持续 | config | 验收 |
|---|---|---|---|
| **影子运行** | 1-3 天 | `http.enabled=false`<br>`feishu.enabled=false` | 只写自己 DB / 存图，不发 MES / 飞书；对比新老版本检测差异 |
| **MES 上线** | 3-7 天 | `http.enabled=true`<br>`feishu.enabled=false` | 新版本推 MES，用新表 `squarstickresult_v2`，MES 切到新表 |
| **完全切换** | 长期 | 全 true | 新版本承担生产；老版本保留 1-2 周再下线 |

---

## 4. 常见问题

### 4.1 启动报"已有一个实例在跑"
- 任务管理器结束所有 `python.exe`
- 还报：删 `D:\SiliconRod_v2\sirod_inspector\logs\main_camera.lock`，再启

### 4.2 启动黑窗 / 找不到 dnninfer.dll
- 检查 `D:\EasyLabel_x64\DeepLearning\` 文件完整
- 检查加密狗插着，license 没过期

### 4.3 UI 起来但相机不动 / 一直"等待触发"
- 检查 BVCam 驱动管理工具能否看到相机
- 检查相机网线 + IP（跟机器同网段）
- 看 log 末尾 `queue.get 超时 (..., 第 1/150 帧, callback 被调 0 次)` → 编码器没转 / 相机没吐图

### 4.4 扫码一直 NoRead（v2 重构最大坑）

按顺序排查：

1. **看 log 启动行是不是 `(iter53 ...`**：
   ```powershell
   Select-String '扫码枪客户端启动' sirod_inspector\logs\sirod_inspector.log | Select-Object -Last 3
   ```
   不是 iter53 → scanner_client.py 没替换最新版，重新部署

2. **看 30 秒心跳**：
   ```powershell
   Get-Content sirod_inspector\logs\sirod_inspector.log -Tail 100 | Select-String 'heartbeat'
   ```
   - `triggers=0` → 我们没在发 `b"start"`，连接出问题
   - `triggers>0 barcodes=0 recv_bytes>0` → 我们在发 trigger，扫码枪在回 ACK，但是没扫到棒（要么没棒、要么扫码枪配置不对）
   - `triggers>0 barcodes=0 recv_bytes=0` → 扫码枪根本不回，连接断了 / 端口被老 Halcon 占着

3. **海康配置自检**：用海康 IDMVS 软件连上扫码枪，检查"TCP 触发文本"是不是字面 `start`、TCP 触发端口是不是 `5000`、触发源是不是 `TCP服务端`

4. **测扫码枪本身**：手动用扫码枪软件触发一次测试扫码，看能不能扫成功

### 4.5 UI 突然卡死（未响应）
- 看 log 找最后一条 `[UI_HB]` 和 `[WORKER_HB]` 时间
- 两个都停 → 进程僵死，任务管理器杀掉重启
- 只一个停 → 那个线程死了，log 找 `CRITICAL` 看 stack
- 反馈日志给开发

### 4.6 NG 大量误报
- UI"参数"tab 调高对应缺陷类别的 `max_area` / `max_length`
- 或临时把 `report_ng` 取消勾选某类别
- 保存即时生效（热加载，不重启）

### 4.7 数据库 / MES 没收到数据
- log 搜 `数据库` / `MES` / `http` 关键字找 ERROR
- 检查 `config.json` 里 `database.host` 和 `http.url` 网络可达
- 临时可关掉 `http.enabled` 让 inspection 继续跑，只是不推 MES

---

## 5. 反馈问题给开发

带齐这 4 样：
1. `D:\SiliconRod_v2\sirod_inspector\logs\sirod_inspector.log` 当天日志
2. `D:\SiliconRod_v2\sirod_inspector\logs\sirod_error.log` 当天错误
3. UI 截图（出错弹窗 / "未响应"等）
4. 文字描述：棒号 / 时间 / 在哪一步出问题

---

## 6. 版本回滚 / 升级

新版本是单仓库，每个迭代有 git commit。升级 = 拉一个新 zip 覆盖，回滚 = 拉老 zip 覆盖。**只覆盖 `sirod_inspector/` 目录**，不要碰 `D:\SiRod_v2\`（存图）和 `config.json`（现场配置）。
