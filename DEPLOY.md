# 部署指南

> 本文档描述如何在工厂机部署「相机驱动模式」(`main_camera.py`)，**完全脱离 Halcon Run.hdev**。
> 原 Halcon 模式 (`main.py`) 仍保留作为可靠回退。

## 一、先决条件

### 软件
| 项 | 版本 / 备注 |
|---|---|
| Python | 3.11+（推荐 3.11 已验证） |
| PyQt6 | UI 框架 |
| numpy / opencv-python / Pillow | 算法层 |
| pymysql / requests / pyserial | 数据库 / 飞书 / 串口 |

```bash
pip install PyQt6 numpy opencv-python Pillow pymysql requests pyserial matplotlib openpyxl psutil
```

### 硬件 / 第三方

| 项 | 安装位置 | 说明 |
|---|---|---|
| **BV 相机驱动** | `C:\Program Files\Bluevision\BVCam\` | DO3THINK BVCam 客户端 |
| **AI 推理 DLL** | `D:\EasyLabel_x64\DeepLearning\` | `dnninfer.dll` + `dnndefine.dll`（带授权） |
| **模型文件** | `<项目>/models/Model_seg.m`、`Model_cls.m` | 训练产物，可由 `config.models.{seg,cls}` 覆盖 |

## 二、初次部署（5 步）

### 1. 检查相机识别

```bash
python tests/smoke_camera.py
```

应看到：
```
枚举到 1 台设备：
  · GigE Bluevision BV-C3110GE  sn=XXXX  ip=...
[1/1] 软触发...
  返回: shape=(15000, 1024) dtype=uint16
[OK] 抓图测试完成
```

> ⚠ 如失败：(1) BVCam Viewer 是否占用相机 (2) 网线/IP/防火墙 (3) GigE 心跳超时需等 3-4 秒再重连

### 2. 检查算法推理

```bash
python tests/smoke_inference.py
```

应看到 `[OK] 烟雾测试通过`，加载 7 类分类 + 4 类分割成功。

### 3. 端到端实拍

```bash
python tests/smoke_live_pipeline.py --shots 3
```

每张 ~1.7s（含 1.2s 抓图 + 0.5s 推理）。

### 4. 准备配置

```bash
cp sirod_inspector/config.example.json sirod_inspector/config.json
# 编辑 config.json，至少改：
#   - database.host / user / password
#   - scanner.host （工厂扫码枪 IP）
#   - serial.port  （COM 口）
#   - feishu / http.url （如启用）
#   - judge.ng_trigger_classes （触发 NG 报警的分类类别）
```

### 5. 启动相机模式

```bash
python sirod_inspector/main_camera.py
```

UI 显示「运行中」绿色徽章 = 一切就位。

## 三、回退到原 Halcon 模式

```bash
python sirod_inspector/main.py
```

`main.py` 完全不依赖本次重构的新模块（`algorithm/`, `camera/`, `core/inspect_engine.py`, `core/scanner_client.py`），可独立运行。

## 四、关键 config 字段速查

```jsonc
{
  "models":  { "seg": "models/Model_seg.m", "cls": "models/Model_cls.m" },
  "camera":  { "width": 1024, "height": 15000,
               "exposure_us": null,              // null = 沿用相机当前值
               "trigger_source": "Software",
               "loop_interval_s": 2.0 },
  "scanner": { "enabled": true,
               "host": "192.168.12.56", "port": 5000,
               "poll_interval_s": 5.0 },
  "judge":   { "max_area": 10, "sum_area": 10,
               "max_count": 10, "max_length": 2,
               "ng_trigger_classes": ["隐裂"] },  // 数组：勾哪几类标 NG
  "alarm":   { "enabled": true }                  // 主界面可勾选
}
```

未填字段 → 用代码默认值（与 Halcon 端原值一致）。

## 五、版本管理 / 回滚

每个迭代都有 git commit，可随时回滚：

```bash
git log --oneline                 # 看所有 iter 节点
git reset --hard <iter-sha>       # 回滚到指定迭代
```

历史：
- `iter0` 算法层完成（preprocess/inference/judge/pipeline）
- `iter1` BV 相机 ctypes 封装
- `iter2` InspectEngine 编排器
- `iter3` `main_camera.py` 平行入口（不动 main.py）
- `iter4` 扫码枪客户端 + NG 类别可配置
- ...

## 六、Sanity check 清单

| 测试脚本 | 验证 | 依赖硬件 |
|---|---|---|
| `tests/smoke_inference.py` | DLL 加载 + 模型推理 | ✗ |
| `tests/smoke_preprocess.py` | 原图 → 1024×3072 | ✗ |
| `tests/smoke_pipeline.py` | preprocess → seg → cc → cls → judge 全链路 | ✗ |
| `tests/smoke_camera.py` | 相机软触发抓图 | ✓ 相机 |
| `tests/smoke_live_pipeline.py` | 相机 → 流水线端到端 | ✓ 相机 |
| `tests/smoke_inspect_engine.py` | InspectEngine 双触发模式 | ✓ 相机 |
| `tests/smoke_scanner.py` | 扫码枪客户端（带 mock 服务器） | ✗ |
| `tests/smoke_inspect_data_contract.py` | InspectData 消费契约 | ✗ |

## 七、常见问题

**Q: 相机 close 后立即重新打开失败？**
A: GigE 心跳超时 ~3-4 秒，等一会再重连。

**Q: cwd 被改了？**
A: 推理 DLL 加载时会强制 `os.chdir(D:\EasyLabel_x64\DeepLearning)`（DLL 加密狗校验所需）。
   `main_camera.py` 内全部用绝对路径，UI/DB/飞书/MES 不受影响。

**Q: 怎么改 NG 触发类别？**
A: 编辑 `config.json` 的 `judge.ng_trigger_classes`，例如 `["隐裂", "崩边"]`。重启生效。

**Q: 想暂时禁用扫码枪？**
A: `"scanner": { "enabled": false }`，所有棒号会标为 `NoRead`。
