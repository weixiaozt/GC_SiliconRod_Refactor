"""
工业读码器 TCP 通信测试脚本
基于《工业读码器通信指令操作手册》

指令格式：
  Get  → <Get,cmdStr>           返回 <Get,cmdStr,value/errno>
  Set  → <Set,cmdStr,param>     返回 <Set,cmdStr,OK/errno>
  Exec → <Exec,cmdStr>          返回 <Exec,cmdStr,OK/errno>

用法：
  python codetcp.py                        # 使用默认 IP/端口
  python codetcp.py --host 192.168.12.100 --port 3000
  python codetcp.py --loop                 # 每 2 秒触发一次，持续输出
  python codetcp.py --loop --interval 1.0 # 自定义间隔
"""

import argparse
import socket
import time

# ── 配置 ───────────────────────────────────────────────────────────────────────
DEFAULT_HOST    = "192.168.12.56"
DEFAULT_PORT    = 4500
DEFAULT_TIMEOUT = 5.0       # 秒

# ── 错误码 ─────────────────────────────────────────────────────────────────────
ERRNO = {
    "0":  "成功",
    "-1": "指令不支持",
    "-2": "参数非法",
    "-3": "命令字符串非法",
    "-4": "设备忙",
    "-5": "执行超时",
    "-6": "未知错误",
    "-7": "目标未使能",
}

# ── socket 工具 ────────────────────────────────────────────────────────────────

def connect(host: str, port: int, timeout: float) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect((host, port))
    return sock


def send_cmd(sock: socket.socket, cmd: str) -> str:
    """发送一条指令，返回响应字符串；超时返回空字符串。"""
    sock.sendall(cmd.encode("utf-8"))
    try:
        return sock.recv(4096).decode("utf-8").strip()
    except socket.timeout:
        return ""


def parse(resp: str) -> tuple[str, str, str]:
    """解析 <type,cmd,value>，返回 (type, cmd, value)。"""
    if resp.startswith("<") and resp.endswith(">"):
        parts = resp[1:-1].split(",", 2)
        if len(parts) == 3:
            return parts[0], parts[1], parts[2]
        if len(parts) == 2:
            return parts[0], parts[1], ""
    return "", "", resp


def explain(value: str) -> str:
    return ERRNO.get(value, value)


# ── 测试函数 ───────────────────────────────────────────────────────────────────

PASS = 0
FAIL = 0

def check(label: str, resp: str, expect_ok: bool = True):
    """打印单条测试结果并统计通过/失败。"""
    global PASS, FAIL
    _, _, val = parse(resp)
    if expect_ok:
        ok = val == "OK" or (val not in ERRNO and val != "")
    else:
        ok = val != ""
    tag = "PASS" if ok else "FAIL"
    desc = explain(val) if val in ERRNO else val
    print(f"  [{tag}]  {label:<32}  {resp}  →  {desc}")
    if ok:
        PASS += 1
    else:
        FAIL += 1
    return val


def section(title: str):
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")


# ── 测试用例 ───────────────────────────────────────────────────────────────────

def test_device_control(sock):
    section("设备控制类")

    # 查询运行模式
    resp = send_cmd(sock, "<Get,RunMode>")
    val = check("Get RunMode", resp, expect_ok=False)
    mode_desc = {"0": "Normal", "1": "Raw", "2": "Test"}.get(val, val)
    print(f"         → 当前模式: {mode_desc}")

    # 设置为 Normal 模式
    check("Set RunMode=Normal(0)", send_cmd(sock, "<Set,RunMode,0>"))

    # 查询采集状态
    resp = send_cmd(sock, "<Get,Acq>")
    val = check("Get Acq", resp, expect_ok=False)
    acq_desc = {"0": "停止采集", "1": "开始采集", "2": "采集中"}.get(val, val)
    print(f"         → 当前状态: {acq_desc}")


def test_user_params(sock):
    section("用户参数类")

    check("Get UserCur",   send_cmd(sock, "<Get,UserCur>"),   expect_ok=False)
    check("Get UserStart", send_cmd(sock, "<Get,UserStart>"), expect_ok=False)
    check("Get Burst",     send_cmd(sock, "<Get,Burst>"),     expect_ok=False)
    check("Get 1DNum",     send_cmd(sock, "<Get,1DNum>"),     expect_ok=False)
    check("Get 2DNum",     send_cmd(sock, "<Get,2DNum>"),     expect_ok=False)

    # 设置 Burst=1
    check("Set Burst=1",   send_cmd(sock, "<Set,Burst,1>"))
    # 设置一维码读码个数=1
    check("Set 1DNum=1",   send_cmd(sock, "<Set,1DNum,1>"))
    # 设置二维码读码个数=1
    check("Set 2DNum=1",   send_cmd(sock, "<Set,2DNum,1>"))
    # 保存参数
    check("Exec UserSave", send_cmd(sock, "<Exec,UserSave>"))


def test_trigger(sock):
    section("触发与 IO 类")

    # 查询触发模式
    resp = send_cmd(sock, "<Get,TriMode>")
    val = check("Get TriMode", resp, expect_ok=False)
    print(f"         → {'触发模式' if val == '1' else '非触发模式'}")

    # 查询触发源
    src_map = {
        "0": "LineIn0", "1": "LineIn1", "2": "LineIn2", "3": "LineIn3",
        "4": "Counter0", "5": "TCP", "6": "UDP", "7": "Software",
        "8": "Serial", "9": "SelfTri", "10": "MainSub", "11": "UsbStart",
        "17": "TOF",
    }
    resp = send_cmd(sock, "<Get,TriSrc>")
    val = check("Get TriSrc", resp, expect_ok=False)
    print(f"         → 触发源: {src_map.get(val, val)}")

    # 切换为触发模式，软件触发源
    check("Set TriMode=触发(1)",    send_cmd(sock, "<Set,TriMode,1>"))
    check("Set TriSrc=Software(7)", send_cmd(sock, "<Set,TriSrc,7>"))

    # 开始采集
    check("Set Acq=开始(1)",        send_cmd(sock, "<Set,Acq,1>"))
    time.sleep(0.2)

    # 软件触发 3 次
    for i in range(3):
        check(f"Exec TriSoft #{i+1}", send_cmd(sock, "<Exec,TriSoft>"))
        time.sleep(0.1)

    # 停止采集
    check("Set Acq=停止(0)", send_cmd(sock, "<Set,Acq,0>"))


def test_code_enable(sock):
    section("读码使能类")

    codes = [
        "ReadAll",
        "Code39", "Code128", "Code93",
        "ITF14", "ITF25",
        "EAN8", "EAN13", "UPCA", "UPCE",
        "Codebar", "MSI", "CNPOST", "Code11", "IND25",
        "PDF417", "QR", "DM", "Matrix25",
        "MicroQR", "AZTEC", "HANXIN",
    ]
    for code in codes:
        resp = send_cmd(sock, f"<Get,{code}>")
        val = check(f"Get {code}", resp, expect_ok=False)
        state = "✓使能" if val == "1" else ("✗关闭" if val == "0" else "?")
        print(f"         → {state}")

    # 开启 QR + DM + Code128
    print()
    check("Set QR=1",      send_cmd(sock, "<Set,QR,1>"))
    check("Set DM=1",      send_cmd(sock, "<Set,DM,1>"))
    check("Set Code128=1", send_cmd(sock, "<Set,Code128,1>"))


def test_smart_tune(sock):
    section("智能调节类")
    check("Exec Tune", send_cmd(sock, "<Exec,Tune>"))


# ── 循环读取模式 ───────────────────────────────────────────────────────────────

def loop_read(sock: socket.socket, interval: float = 2.0):
    """
    每隔 interval 秒发送一次软件触发，接收并打印返回的数据。
    先将设备切换为触发模式（软件触发源）并开始采集。
    按 Ctrl+C 退出。
    """
    print("\n正在初始化触发模式 ...")
    for cmd, desc in [
        ("<Set,TriMode,1>", "触发模式"),
        ("<Set,TriSrc,7>",  "Software 触发源"),
        ("<Set,Acq,1>",     "开始采集"),
    ]:
        resp = send_cmd(sock, cmd)
        _, _, val = parse(resp)
        status = "OK" if val == "OK" else f"失败({explain(val)})"
        print(f"  {desc}: {status}")

    print(f"\n开始循环读取，间隔 {interval} 秒，按 Ctrl+C 停止\n")
    print(f"{'时间':<10}  {'#':<5}  {'触发':<8}  条码数据")
    print("─" * 60)

    count = 0
    try:
        while True:
            count += 1
            ts = time.strftime("%H:%M:%S")

            # 1. 发送软件触发
            sock.sendall(b"<Exec,TriSoft>")

            # 2. 读取触发确认（<Exec,TriSoft,OK>）
            try:
                ack = sock.recv(4096).decode("utf-8").strip()
            except socket.timeout:
                ack = "TIMEOUT"

            _, _, val = parse(ack)
            trig_ok = val == "OK"

            # 3. 触发成功后读取条码结果（设备扫描完成后主动推送）
            barcode = ""
            if trig_ok:
                try:
                    sock.settimeout(interval * 0.8)   # 最多等 80% 间隔时间
                    barcode = sock.recv(4096).decode("utf-8").strip()
                except socket.timeout:
                    barcode = "（无数据 / 未识别）"
                finally:
                    sock.settimeout(DEFAULT_TIMEOUT)

            trig_mark = "OK" if trig_ok else f"ERR({val})"
            print(f"{ts}  [{count:>4}]  {trig_mark:<8}  {barcode}")

            time.sleep(interval)

    except KeyboardInterrupt:
        print(f"\n已停止，共触发 {count} 次")
    finally:
        send_cmd(sock, "<Set,Acq,0>")
        print("采集已停止")


# ── 主程序 ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="工业读码器 TCP 通信测试")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--loop", action="store_true",
                        help="循环读取模式，每隔 interval 秒触发一次")
    parser.add_argument("--interval", type=float, default=2.0,
                        help="循环读取间隔秒数（默认 2.0）")
    args = parser.parse_args()

    print(f"\n正在连接 {args.host}:{args.port} ...")
    try:
        sock = connect(args.host, args.port, args.timeout)
    except Exception as e:
        print(f"连接失败: {e}")
        return

    print(f"连接成功\n")

    try:
        if args.loop:
            loop_read(sock, args.interval)
        else:
            test_device_control(sock)
            test_user_params(sock)
            test_trigger(sock)
            test_code_enable(sock)
            test_smart_tune(sock)
    finally:
        sock.close()
        if not args.loop:
            print(f"\n{'═'*60}")
            print(f"  测试完成   PASS: {PASS}   FAIL: {FAIL}")
            print(f"{'═'*60}\n")


if __name__ == "__main__":
    main()
