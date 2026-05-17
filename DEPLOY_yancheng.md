# 盐城 SiRod Inspector 新版本部署手册

新版本（Python 重构版）和老版本（Halcon+Python）**完全隔离 / 互不影响 / 可随时切换回老版本兜底**。

---

## 目录结构（部署后）

```
D:\
├── silicon-rod-defect-review\      ← 老版本（保持原状，本次不动）
│   └── ...
│
├── SiliconRod_v2\                  ← 新版本（本次部署）
│   ├── sirod_inspector\
│   │   ├── main_camera.py
│   │   ├── config.json             ← 从 config.v2.example.json 拷贝并改
│   │   └── logs\
│   │       └── main_camera.lock    ← 单实例锁（自动管理）
│   ├── models\
│   │   ├── Model_seg.m             ← 分割模型
│   │   └── Model_cls.m             ← 新版分类模型
│   └── tests\                      ← 测试脚本（可选）
│
├── SiRod\                          ← 老版本存图（本次不动）
│   ├── images\
│   ├── ImageRaw\
│   └── WebImage\
│
└── SiRod_v2\                       ← ★ 新版本独立存图位（首次启动自建）
    ├── images\
    ├── ImageRaw\
    └── WebImage\

C:\Program Files\Bluevision\BVCam\  ← 相机驱动（共用）
D:\EasyLabel_x64\DeepLearning\      ← AI 推理 DLL（共用，只读）
```

---

## 部署步骤（按顺序做）

### 1. 检查盐城机预装

| 项 | 命令 / 检查 |
|---|---|
| Python 3.11 | `python --version` → 3.11.x |
| Windows 长路径 | `regedit` → `HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem!LongPathsEnabled = 1`（重启生效） |
| 相机驱动 | 确认 `C:\Program Files\Bluevision\BVCam\Driver\BVCam.dll` 存在 |
| AI 推理 DLL | 确认 `D:\EasyLabel_x64\DeepLearning\dnninfer.dll` + `dnndefine.dll` 存在 |
| MySQL 可连 | 用 navicat / mysql cli 测一下 `b_xmartsql` 库可访问 |

### 2. 装 Python 依赖

```bat
pip install PyQt6 opencv-python numpy psutil pymysql requests
```

如果走 Halcon 模式（旧 main.py 兜底用），再加：

```bat
pip install mvtec-halcon
```

### 3. 拷贝代码 + 模型

把开发机 `D:\Project\SiliconRod_refactor\` 全部拷到盐城机 `D:\SiliconRod_v2\`：

```
必带：
  sirod_inspector\        # 全部代码
  models\Model_seg.m      # 分割模型
  models\Model_cls.m      # 新版分类模型
  scripts\deploy\         # 启动脚本
  DEPLOY_yancheng.md      # 这份文档

可不带：
  source_image\           # 14000+ 张训练原图，太大，部署不用
  test_image\             # 测试图，部署不用
  tests\                  # 测试脚本，部署不需要
  .git\                   # 仓库元数据，部署不需要
```

### 4. 初始化 config.json

```bat
cd D:\SiliconRod_v2\sirod_inspector
copy config.v2.example.json config.json
notepad config.json
```

按现场改 5 处：
- `database.host` / `password` — 现场 MySQL
- `serial.port` — 报警灯 COM 口
- `image_store.web_url_base` — MES 拉图的 HTTP 根 URL（你现场服务器 IP）
- `line_id` — 产线号
- 其它 IP / 端口按需

★ **`http.enabled` 和 `feishu.enabled` 保持 false**，调试期不要往 MES / 飞书推。等新版本跑稳定（建议跟踪 3-7 天），再翻 true。

### 5. 部署启动脚本到桌面

把 `D:\SiliconRod_v2\scripts\deploy\新版本.bat` 复制到桌面。

老版本启动脚本（如果没有的话）：
- 编辑 `老版本.bat.template`，把 "改成老版本实际路径" 替换成你老版本目录（如 `D:\silicon-rod-defect-review`），改启动命令（如 `python main.py`）
- 另存为 `老版本.bat` 到桌面

桌面应该看到两个图标：
- 🟢 `老版本.bat` — 兜底，生产用
- 🆕 `新版本.bat` — 调试 / 上线后用

### 6. 首次启动测试

**先关掉所有 python.exe**（任务管理器查），然后双击 `新版本.bat`。

预期看到（按时序）：
1. 控制台显示 "新版本 启动中"
2. Python 启动日志输出
3. 模型加载 ~20-30s
4. PyQt 主窗口弹出
5. 顶部右上"扫码枪在线"灯先红再绿（或保持红如果扫码枪没接）
6. 状态徽章 "等待触发"

来一棒真品扫码 + 进相机 ROI → 完整跑一次：
- 总览页右上 检测数 +1
- 实时预览框显示带 mask / 红框 / "隐裂 0.95" 之类标签的大图
- 数据库 `squarstickresult_v2` 表 +1 行
- `D:\SiRod_v2\images\<日期>\full\` 下出现 raw / marked 大图
- `D:\SiRod_v2\ImageRaw\` 下出现 uint16 TIF 原图

NG 流程：
- 顶部状态徽章变红显示 "NG: 棒号 / 类型"
- 报警灯硬件输出（串口 send_ng 触发）
- 缺陷图库 tab 自动加一项
- `D:\SiRod_v2\images\<日期>\crops\` 下出现该缺陷的 raw + marked 小图

---

## 切换流程（操作员日常）

### 切到新版本
1. 任务管理器 → 找所有 `python.exe` → 全部结束
2. 等 5 秒（让相机驱动释放干净）
3. 双击桌面 🆕 `新版本.bat`

### 切回老版本（如果新版有问题）
1. 任务管理器 → 找所有 `python.exe` → 全部结束
2. 等 5 秒
3. 双击桌面 🟢 `老版本.bat`

★ **不要同时跑两套** — 会撞相机 / 串口 / 扫码枪硬件，互相抢资源；同时还会双写存图 / 同步 MES。

---

## 上线 → 稳定 → 切量 路线图

| 阶段 | 持续时间 | 配置 | 验收标准 |
|---|---|---|---|
| **影子运行** | 1-3 天 | `http.enabled=false` `feishu.enabled=false` | 新版本跑产线但只写自己的 DB 表 / 存图，不发 MES / 飞书；对比新老版本检测结果差异，记录漏检 / 误判 |
| **MES 上线** | 3-7 天 | `http.enabled=true` `feishu.enabled=false` | 新版本开始往 MES 推数据；用新表名 `squarstickresult_v2`，MES 侧 query 切到新表 |
| **完全切换** | 长期 | `http.enabled=true` `feishu.enabled=true` | 新版本承担生产；老版本保留可回滚 1-2 周再下线 |

---

## 应急 / 常见问题

### 启动报"已有一个 SiRod Inspector 实例在跑"
- 任务管理器找 `python.exe` 杀掉
- 还报：删 `D:\SiliconRod_v2\sirod_inspector\logs\main_camera.lock`，再启

### 启动黑窗 / 找不到 dnninfer.dll
- 检查 `D:\EasyLabel_x64\DeepLearning\` 文件完整
- 检查 license 是否到期

### UI 起来但相机不动 / 一直"等待触发"
- 检查 BVCam 驱动管理工具能否看到相机
- 检查相机 GigE 网线 + IP（同网段）
- 看 `sirod_inspector/logs/sirod_inspector.log` 末尾 ERROR

### UI 突然 "（未响应）" / 卡死
- 看 log 找最后一条 `[UI_HB]` 和 `[WORKER_HB]` 时间
- 两个都停 → 整个进程僵 → 任务管理器杀掉，重启
- 只一个停 → 那个线程死了 → log 找 `CRITICAL` 看 stack
- 反馈日志给开发，附 log 文件

### NG 大量误报
- 切到"参数" tab，调高 隐裂 的 `max_area` / `max_length`
- 或临时把 `report_ng` 取消勾选某类别
- 保存即时生效（iter24 热加载），不用重启

### 数据库 / MES 没收到数据
- 看 log 找 `数据库`、`MES`、`http` 关键字的 ERROR
- 检查 config.json 的 `database.host` 和 `http.url` 能否网络可达
- 临时可关掉 `database.enabled` 或 `http.enabled` 让程序不崩

---

## 联系开发

出问题反馈带：
1. `D:\SiliconRod_v2\sirod_inspector\logs\sirod_inspector.log` 当天的日志
2. `D:\SiliconRod_v2\sirod_inspector\logs\sirod_error.log` 当天的错误日志
3. UI 截图（"未响应" / 异常窗口）
4. 棒号 / 时间 / 在哪一步出问题
