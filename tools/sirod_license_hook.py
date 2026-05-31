"""
软件授权启动钩子（site 注入版）—— ★不动现场一行 app 源码★ 就给主程序上授权锁。
=====================================================================
原理：Python 启动时会自动加载 site-packages 里的 ``.pth``，我们丢一个一行的
``sirod_lock.pth``（内容：``import sirod_license_hook``）进去，Python 每次起来都会
执行本钩子。本钩子只在**启动 main_camera.py / main.py 时**校验授权，其它 python
调用（pip 等）一律放行——所以不会误锁整个环境。

部署（丢进现场 Python 的 site-packages，全程不碰 app 源码）：
  1. license_guard.<abi>.pyd        ← 锁逻辑（你已按版本编好：盐城 cp310 / 宜宾 cp311）
  2. sirod_license_hook.py          ← 本文件（也可编译成 .pyd 加固，非必须）
  3. sirod_lock.pth                 ← 一行：import sirod_license_hook
  另：pip install cryptography；license.dat 放项目根（程序 cwd）。

强度说明（诚实）：相比"源码内置闸门"，本方式可被 ``python -S``（跳过 site 初始化）
绕过，site-packages 里这三个文件本身也是 weak link。它防的是操作工 / 私自整盘复制，
不是硬核逆向。要更硬就用源码内置闸门 + 编译，或把本钩子也编译成 .pyd。
"""

import os
import sys


def _enforce() -> None:
    # 只锁主程序；pip / 其它 python 调用一律放行，避免把整个 Python 环境锁死
    script = os.path.basename((sys.argv[0] if sys.argv else "") or "").lower()
    if script not in ("main_camera.py", "main.py"):
        return

    try:
        import license_guard  # 同在 site-packages 里的 .pyd（锁逻辑+内嵌公钥）
    except Exception:  # noqa: BLE001
        # 找不到锁逻辑（部署不全）→ 不阻断，避免把所有 python 调用都砖掉
        return

    try:
        status = license_guard.check_license()
    except Exception:  # noqa: BLE001
        return

    if status.ok:
        if getattr(status, "warn", False):
            try:
                license_guard._message_box(
                    f"SiRod Inspector 授权即将到期\n\n{status.message}\n\n请尽快联系开发续期。",
                    "SiRod 授权提醒", error=False)
            except Exception:  # noqa: BLE001
                pass
        return

    # 校验未通过 → 弹原生框 + 立即退出。
    # 用 os._exit(0)：site 初始化阶段 sys.exit 的 SystemExit 行为不稳；_exit 立即干净退出，
    # 退码 0 让 launcher 当“正常退出”，不触发 5 次重启刷屏（见 launcher.py 退出码约定）。
    try:
        license_guard._message_box(
            f"SiRod Inspector 无法启动 —— 软件授权校验未通过。\n\n"
            f"原因：{status.message}\n\n请联系开发获取/更新授权文件 license.dat。",
            "SiRod 授权校验失败", error=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass
    os._exit(0)


_enforce()
