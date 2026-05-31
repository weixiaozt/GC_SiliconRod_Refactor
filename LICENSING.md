# 软件授权锁（license_guard）使用手册

给 SiRod Inspector 加的「软件锁」：**绑机器 + 到期** 的离线授权。未授权机器/到期后
无法启动，挡住私自拷贝到别的产线、以及到期不续。

- **机制**：Ed25519 非对称签名。现场只有 *公钥*（内嵌在 `license_guard`），签发用的
  *私钥* 永远在你手里、离线保管。源码全暴露也伪造不出有效授权。
- **绑机器**：用主板/CPU/BIOS/磁盘序列号算机器锚点，授权写死锚点，换机/整盘拷贝就失效。
  容忍 1 项硬件变化（修机换块盘不至于直接砖）。
- **防改钟**：到期日不能靠回拨系统时间绕过（HKCU 存「见过的最晚日期」水位线）。
- **tier B 编译保护**：`license_guard.py` 编译成 `license_guard.pyd` 发到现场（删 .py），
  验签逻辑和公钥藏在二进制里，不是记事本能删的明文。

> ⚠️ **诚实的强度边界**：现场跑的是 Python。tier B 把*授权模块*编成二进制，挡住绝大多数
> 人；但调用它的 `main_camera.py` 仍是明文，懂行且拿到机器物理访问权的人，理论上仍可
> patch 掉调用。它防的是「私自复制、到期赖着用、产线操作工」，不是国家级逆向。要再硬就
> 整体 PyInstaller 打包（当前未采用，避免动现场部署模型）。

---

## 〇、现场实操速查（照着做）

> **两个现场 Python 版本不同 —— 发各自版本的 `.pyd`，拿错版本直接加载失败：**
>
> | 现场 | Python | 发这台的 `.pyd` |
> |---|---|---|
> | **盐城** | 3.10 | `build\license_guard_310\license_guard.cp310-win_amd64.pyd` |
> | **宜宾** | 3.11 | `build\license_guard_311\license_guard.cp311-win_amd64.pyd` |
>
> 步骤标了在哪台机器做：`[你的电脑]`=开发机（有私钥、能编译）；
> `[盐城电脑]`/`[宜宾电脑]`=对应现场工控机。
> **每个现场各有自己的 license.dat（绑各自机器），分开存档、绝不能混用。**

### 准备（只做一次，两个现场共用同一套私钥/公钥）

`[你的电脑]`
```bat
:: 1. 装工具
pip install cryptography nuitka

:: 2. 生成私钥 + 公钥（私钥★离线留好★，泄露=所有授权作废）
python tools\license_gen.py keygen
::    → private_key.pem，并打印一行  _PUBLIC_KEY_HEX = "xxxx..."

:: 3. 把那行公钥覆盖进 sirod_inspector\core\license_guard.py 的 _PUBLIC_KEY_HEX

:: 4. 各现场各编一份 .pyd（★--no-project 必加，否则会把你的 .venv 重建成别的版本★）
::    盐城 cp310：
uv run --no-project --python 3.10 --with nuitka python -m nuitka --module ^
  sirod_inspector/core/license_guard.py --output-dir=build/license_guard_310 ^
  --remove-output --assume-yes-for-downloads
::    宜宾 cp311：
uv run --no-project --python 3.11 --with nuitka python -m nuitka --module ^
  sirod_inspector/core/license_guard.py --output-dir=build/license_guard_311 ^
  --remove-output --assume-yes-for-downloads
```

---

# ▌▌ 盐城（Python 3.10 → cp310） ▌▌

### 场景 A — 第一次给盐城加锁

`[盐城电脑]`
```bat
:: A1. 把 license_guard.cp310-win_amd64.pyd 拷到  sirod_inspector\core\
::     （旧版本本来没有 license_guard.py，不用删任何东西）

:: A2. 编辑 sirod_inspector\main_camera.py，在 def main(): 第一行加这两行：
::         from core.license_guard import verify_or_exit
::         verify_or_exit()

:: A3. 装依赖
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple cryptography

:: A4. 取本机机器码，复制打印的那一整行 blob 发给你（开发）
python tools\get_machine_id.py
```

`[你的电脑]`（拿到盐城发来的 blob 后）
```bat
:: A5. 签发授权（示例：用到 2027-06-30）
python tools\license_gen.py issue --machine <盐城发来的blob> --expires 2027-06-30 --customer "盐城"
::     → 产出 license.dat
::  ★把这份 license.dat（和那个 blob）自己存一份档，续期要用★
```

`[盐城电脑]`
```bat
:: A6. 把收到的 license.dat 放到项目根目录（与 sirod_inspector\ 同级），启动即生效。
```

### 场景 B — 到期了，给盐城续时长

盐城到期前 15 天，每次启动会自动弹「还剩 X 天」提醒。续期**不用盐城重新取码**：

`[你的电脑]`（用场景 A 存档的盐城 license.dat）
```bat
:: 续到 2028-06-30：
python tools\license_gen.py renew --in 盐城-license.dat --expires 2028-06-30
:: 或：从今天起再给一年
python tools\license_gen.py renew --in 盐城-license.dat --days 365
::     → 产出新的 license.dat
```

`[盐城电脑]`
```bat
:: 用新的 license.dat 覆盖项目根里的旧 license.dat，重启程序即生效。
```

> 万一旧 license.dat 没存档：让盐城重跑 `python tools\get_machine_id.py` 给你新 blob，
> 再 `issue --expires <新日期>` 即可（硬件没变，机器码一样）。

### 场景 C — 给盐城改成永不到期

「永不到期」= 这台盐城机器永久能跑，但仍然**只绑这台机器**（拷到别的机器照样不行）。

`[你的电脑]`
```bat
python tools\license_gen.py renew --in 盐城-license.dat --expires none
::     → 产出新的 license.dat（永久授权）
```

`[盐城电脑]`
```bat
:: 用新的 license.dat 覆盖旧的，重启即生效。从此不再有到期。
```

> 想**彻底拆锁**（任何机器都能跑、等于没锁）：删盐城 `main_camera.py` 里加的那两行 +
> 删 `sirod_inspector\core\license_guard.cp310-win_amd64.pyd`。这是卸载锁，不是授权。

---

# ▌▌ 宜宾（Python 3.11 → cp311） ▌▌

> 和盐城步骤一模一样，差别只有三处：**用 cp311 的 .pyd**、**备注写「宜宾」**、
> **存宜宾自己那份 license.dat（和盐城分开放）**。

### 场景 A — 第一次给宜宾加锁

`[宜宾电脑]`
```bat
:: A1. 把 license_guard.cp311-win_amd64.pyd 拷到 sirod_inspector\core\
::     （旧版本本来没有 license_guard.py，不用删任何东西）

:: A2. 编辑 sirod_inspector\main_camera.py，在 def main(): 第一行加这两行：
::         from core.license_guard import verify_or_exit
::         verify_or_exit()

:: A3. 装依赖
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple cryptography

:: A4. 取本机机器码，复制打印的那一整行 blob 发给你（开发）
python tools\get_machine_id.py
```

`[你的电脑]`（拿到宜宾发来的 blob 后）
```bat
:: 示例：用到 2027-06-30
python tools\license_gen.py issue --machine <宜宾发来的blob> --expires 2027-06-30 --customer "宜宾"
::     → 产出 license.dat
::  ★把宜宾这份 license.dat（和那个 blob）单独存档，和盐城的分开放，续期要用★
```

`[宜宾电脑]`
```bat
:: 把收到的 license.dat 放到项目根目录（与 sirod_inspector\ 同级），启动即生效。
```

### 场景 B — 到期了，给宜宾续时长

宜宾到期前 15 天，每次启动会自动弹「还剩 X 天」提醒。续期**不用宜宾重新取码**：

`[你的电脑]`（用场景 A 存档的宜宾 license.dat）
```bat
:: 续到 2028-06-30：
python tools\license_gen.py renew --in 宜宾-license.dat --expires 2028-06-30
:: 或：从今天起再给一年
python tools\license_gen.py renew --in 宜宾-license.dat --days 365
::     → 产出新的 license.dat
```

`[宜宾电脑]`
```bat
:: 用新的 license.dat 覆盖项目根里的旧 license.dat，重启程序即生效。
```

> 万一旧 license.dat 没存档：让宜宾重跑 `python tools\get_machine_id.py` 给你新 blob，
> 再 `issue --expires <新日期> --customer "宜宾"` 即可（硬件没变，机器码一样）。

### 场景 C — 给宜宾改成永不到期

「永不到期」= 这台宜宾机器永久能跑，但仍然**只绑这台机器**（拷到别的机器照样不行）。

`[你的电脑]`
```bat
python tools\license_gen.py renew --in 宜宾-license.dat --expires none
::     → 产出新的 license.dat（永久授权）
```

`[宜宾电脑]`
```bat
:: 用新的 license.dat 覆盖旧的，重启即生效。从此不再有到期。
```

> 想**彻底拆锁**（任何机器都能跑、等于没锁）：删宜宾 `main_camera.py` 里加的那两行 +
> 删 `sirod_inspector\core\license_guard.cp311-win_amd64.pyd`。这是卸载锁，不是授权。

---

# ▌▌ 两个现场公共说明 ▌▌

### 到期日怎么算（避免误会）

`--expires 2026-06-30` = **6 月 30 日当天还能用**，**7 月 1 日起被拦**。到期日是「最后一个能用的日子」。
今天往后推几天就是几天：今天 `2026-05-31` 写 `--expires 2026-06-30` 只有 30 天。常用：
一年 `--expires 2027-06-30`、两年 `--days 730`、永久 `--expires none`。

---

## 一、一次性准备（你，厂商侧，只做一次）

```bat
:: 1) 装工具
pip install cryptography nuitka

:: 2) 生成密钥对（私钥务必离线留存，泄露=全部授权作废）
python tools\license_gen.py keygen
::   → 写出 private_key.pem
::   → 打印一行  _PUBLIC_KEY_HEX = "xxxx..."

:: 3) 把打印那行覆盖进 sirod_inspector\core\license_guard.py 的 _PUBLIC_KEY_HEX

:: 4) 编译成 .pyd（tier B）★必须用与目标机相同的 Python 版本编★
::    .pyd 带 ABI 标签：cp310 只能在 Python 3.10 上加载，cp312 只能在 3.12 上。
::    本机(3.12)给 3.12 机器编：
scripts\deploy\build_license_guard.bat
::    给盐城(3.10)编 —— 用 uv 拉 3.10，★加 --no-project 否则会把你的 .venv 重建成 3.10★：
uv run --no-project --python 3.10 --with nuitka python -m nuitka --module ^
  sirod_inspector/core/license_guard.py --output-dir=build/license_guard_310 ^
  --remove-output --assume-yes-for-downloads
::   → build\license_guard_310\license_guard.cp310-win_amd64.pyd
```

> 不确定目标机 Python 版本就先去那台敲 `python --version`。版本不对 `.pyd` 直接加载失败。

> `private_key.pem` 已被 `.gitignore` 排除。**再强调：私钥不进 git、不进现场、离线备份。**

---

## 二、给一台机器签发授权（每台机器一次）

```
┌─ 现场工控机 ─────────────┐        ┌─ 你（私钥）──────────────┐
│ python tools\get_machine_id.py │  blob  │ python tools\license_gen.py    │
│   → 打印机器码 blob          │ ──────▶│   issue --machine <blob>       │
│                              │        │   --expires 2027-05-31         │
│ 收到 license.dat 放项目根    │◀────── │   → 产出 license.dat            │
└──────────────────────────┘ license └────────────────────────────┘
```

现场（工控机）：
```bat
python tools\get_machine_id.py
:: 复制打印的那一整行 blob 发给你
```

你（私钥侧）：
```bat
:: 一年期
python tools\license_gen.py issue --machine <blob> --expires 2027-05-31 --customer "盐城-1线"
:: 或 从今天起 N 天
python tools\license_gen.py issue --machine <blob> --days 365 --customer "盐城-1线"
:: 永久（仅绑机器，不到期）
python tools\license_gen.py issue --machine <blob> --expires none --customer "盐城-1线"
:: → 产出 license.dat，发回现场放到项目根目录
```

到期前 15 天（默认，`--warn-days` 可调）启动会弹「还剩 X 天」提醒但仍放行；到期硬停。

---

## 三、部署到现场（含回迁旧生产版本）

锁与检测逻辑完全解耦，回迁旧版只需 4 步、不碰任何检测代码：

1. 拷 `license_guard.<目标版本>.pyd`（盐城 3.10 = `cp310-win_amd64`）→ 现场
   `sirod_inspector\core\`；现场若已有 `license_guard.py` 一并删掉（只留 .pyd），
   旧版本本来没有则不用管。
2. `main_camera.py` 的 `main()` 最前面加两行（新版本已含）：
   ```python
   from core.license_guard import verify_or_exit
   verify_or_exit()
   ```
3. `pip install cryptography`（清华源，见 DEPLOY.md）。
4. 把签发好的 `license.dat` 放到项目根目录。

启动即校验。

---

## 三·B、完全不改源码的部署（site 钩子，免维护）

和上面「方式 A」（拷 .pyd + 改 `main_camera.py` 两行）相比，这种方式 **一行 app 源码都不改**，
靠 Python 启动钩子生效；现场以后升级 app 版本也不用再打补丁。已在全新 venv 实测通过。

**原理**：Python 启动会自动加载 site-packages 里的 `.pth`。丢一个一行的 `sirod_lock.pth`
（内容 `import sirod_license_hook`）进去，每次 python 启动就执行钩子；钩子**只在启动
`main_camera.py` / `main.py` 时**校验授权，`pip` 等其它调用一律放行（不会误锁整个环境）。

**部署**（把 3 个文件丢进现场 Python 的 site-packages，全程不碰 app 源码）：

| 文件 | 来源 | 说明 |
|---|---|---|
| `license_guard.<abi>.pyd` | 你按版本编的（盐城 cp310 / 宜宾 cp311） | 锁逻辑 + 内嵌公钥 |
| `sirod_license_hook.py` | `tools/sirod_license_hook.py` | 钩子（**两个现场通用**，纯 .py 不分版本；想加固可编成 .pyd） |
| `sirod_lock.pth` | `tools/sirod_lock.pth` | 一行 `import sirod_license_hook` |

```bat
:: 现场：找到 site-packages 路径
python -c "import site; print(site.getsitepackages()[-1])"
:: 把上面 3 个文件拷进该目录；再装依赖：
pip install -i https://pypi.tuna.tsinghua.edu.cn/simple cryptography
:: license.dat 放项目根（程序启动时的 cwd），启动即生效。★不用动 main_camera.py★
```

> **方式 A vs 方式 B**：A 把闸门写进 app（直接 `python main_camera.py` 也拦得住，最稳），
> 但每次升级 app 要重新加那两行；B 完全不碰源码、随 app 升级免维护，代价是能被
> `python -S`（跳过 site 初始化）绕过、site-packages 里那 3 个文件也是 weak link。
> 防操作工 / 私自复制足够；要最硬用 A，或两者叠加。**用 B 就别再改 `main_camera.py`**（二选一）。

---

## 四、续期 / 永久 / 拆锁 / 换机

- **续期（推荐，无需现场重新取码）** —— 机器锚点不变，只换到期日重签：
  ```
  python tools\license_gen.py renew --in license.dat --expires 2028-12-31
  python tools\license_gen.py renew --in license.dat --days 365
  ```
  新 `license.dat` 发回现场覆盖旧的。**前提：你手上有那台机器的旧 license.dat 副本
  —— 每次签发都留档**（或留着它的机器码 blob，硬件不变可随时重签）。
- **改永久授权**（仍绑这台机器、永不到期）：
  ```
  python tools\license_gen.py renew --in license.dat --expires none
  ```
- **彻底拆锁**（任何机器都能跑 = 等于没锁，不是授权）：删现场 `main_camera.py` 的两行
  gate + 删 `sirod_inspector\core\license_guard.pyd`。
- **换机/换主板/换硬盘**：机器码变 → 现场重新 `get_machine_id` → 重新 `issue`。
  （换 1 项硬件通常仍能通过，容忍度 = 锚点数 - 1。）
- **到期前 15 天**现场启动会自动弹「还剩 X 天」提醒（放行），给你提前续期的窗口。

---

## 五、严格性矩阵（重要）

| 运行形态 | 行为 |
|---|---|
| 编译 `.pyd`（生产） | **永远严格**，校验不过弹框 + `sys.exit(0)` 拒启动 |
| 源码 `.py`（你的开发机） | **默认放行** + 打 WARNING —— 开发自己不会被锁死 |
| 源码 `.py` + 环境变量 `SIROD_LICENSE_STRICT=1` | 临时严格，用来本机测拦截逻辑 |

退出码故意用 **0**：launcher 把 0 当「正常退出」，不会触发 5 次重启刷屏
（见 `scripts/deploy/launcher.py` 退出码约定）。

---

## 六、故障排查（启动弹「授权校验失败」时看 message code）

| code | 含义 | 处理 |
|---|---|---|
| `NO_PUBKEY` | 没配公钥（还是占位符） | 编译前忘了粘公钥；重走「一次性准备」 |
| `NO_CRYPTO` | 缺 cryptography | `pip install cryptography` |
| `NO_LICENSE` | 没找到 license.dat | 把签发的 license.dat 放项目根 |
| `BAD_FORMAT` | license.dat 损坏 | 重新签发 |
| `BAD_SIGNATURE` | 被篡改 / 公私钥不配对 | 确认是本套私钥签的；重新签发 |
| `MACHINE_MISMATCH` | 授权绑的不是这台机器 | 换机/换硬件了 → 重新取机器码签发 |
| `EXPIRED` | 已过期 | 续期重签 |

> license.dat 路径：环境变量 `SIROD_LICENSE` > 项目根 `license.dat` > `sirod_inspector\license.dat`。

回归测试：`uv run python tests\smoke_license_guard.py`（覆盖全部判定分支）。

---

## 七、文件清单 — 哪些进 git，哪些绝不进

| 文件 | 进 git？ | 说明 |
|---|---|---|
| `sirod_inspector/core/license_guard.py` | ✅（占位公钥） | 源码，公钥处留占位符 |
| `tools/license_gen.py` / `tools/get_machine_id.py` | ✅ | 工具 |
| `scripts/deploy/build_license_guard.bat` | ✅ | 构建脚本 |
| `tests/smoke_license_guard.py` | ✅ | 回归测试 |
| `private_key.pem` | ❌ **绝不** | 私钥，泄露=全部授权作废 |
| `license.dat` | ❌ | 每台机器签发，按机部署 |
| `license_guard*.pyd` | ❌ | 编译产物（含真实公钥），按需编译部署 |

（后三类已在 `.gitignore` 排除。）
