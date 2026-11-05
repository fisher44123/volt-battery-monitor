"""
导出本机诊断报告，用于 GitHub issue 里说明「我的设备为什么读不到电」。

借鉴 myfinder-device-battery 的程序内反馈机制（对方 86 个 issue 大多由
结构化诊断自动生成），但这里做成纯本地、零联网、零上传的脚本：
只生成一个 txt 文件，用户自己决定要不要贴出去。

用法（用装了 hidapi 的 Python）：
    C:\\Python313\\python.exe scripts\\export_diagnostics.py

输出：data/diagnostics-<时间戳>.txt
"""
import json
import os
import platform
import sys
import time
from datetime import datetime

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)
os.chdir(PROJ)

DATA_DIR = os.path.join(PROJ, "data")
DEVICES_DIR = os.path.join(PROJ, "devices")


def redact(text: str) -> str:
    """把本机用户名等私有路径抹掉，报告可以安全公开"""
    home = os.path.expanduser("~")
    for raw in (home, home.replace("\\", "/")):
        if raw:
            text = text.replace(raw, "<HOME>")
    return text


def out(lines, text=""):
    lines.append(redact(str(text)))


def main():
    lines = []
    out(lines, "=" * 68)
    out(lines, "Volt 外设电量监控 · 本机诊断报告")
    out(lines, f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    out(lines, "=" * 68)
    out(lines)
    out(lines, "[ 运行环境 ]")
    out(lines, f"  Python      : {sys.version.split()[0]} ({platform.architecture()[0]})")
    out(lines, f"  Windows     : {platform.platform()}")
    out(lines, f"  运行方式    : {'打包 exe' if getattr(sys, 'frozen', False) else '源码'}")
    try:
        import hid
        out(lines, f"  hidapi      : {getattr(hid, '__version__', '已安装')}")
    except Exception as e:
        out(lines, f"  hidapi      : 导入失败 -> {e}")
    out(lines)

    # ── 设备配置 ──
    out(lines, "[ 已启用的设备配置 ]")
    configs = []
    try:
        for fname in sorted(os.listdir(DEVICES_DIR)):
            if not fname.endswith(".json") or fname == "template.json":
                continue
            with open(os.path.join(DEVICES_DIR, fname), "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if not cfg.get("enabled", True):
                continue
            configs.append(cfg)
            out(lines, f"  {cfg.get('id')}: {cfg.get('name')}")
            out(lines, f"    VID/PID      : {cfg.get('vendor_id')} / {', '.join(cfg.get('product_ids', []))}")
            out(lines, f"    集合          : {cfg.get('usage_page')}:{cfg.get('usage')}")
            interval = cfg.get("poll_interval")
            interval_text = f"{interval}s" if interval else "由协议决定"
            out(lines, f"    协议/轮询     : {cfg.get('protocol')} / {interval_text}")
            out(lines, f"    产品名白名单  : {cfg.get('product_names', ['(未限制)'])}")
            out(lines, f"    证据等级      : {cfg.get('evidence_level', '未标注')} — {cfg.get('evidence_note', '')}")
    except Exception as e:
        out(lines, f"  读取配置失败: {e}")
    out(lines)

    # ── 相关 HID 接口 ──
    out(lines, "[ 本机相关 HID 接口（判定设备能否被识别的依据）]")
    wanted_vids = set()
    for cfg in configs:
        try:
            wanted_vids.add(int(str(cfg.get("vendor_id", "0")), 16))
        except Exception:
            pass
    try:
        import hid
        found = []
        for d in hid.enumerate():
            vid = d.get("vendor_id")
            if vid not in wanted_vids:
                continue
            path = d.get("path")
            path_s = path.decode("latin1", errors="ignore") if isinstance(path, bytes) else str(path)
            found.append((vid, d.get("product_id"), d.get("usage_page"),
                          d.get("usage"), d.get("interface_number"),
                          d.get("product_string"), path_s))
        if not found:
            out(lines, "  (未发现任何相关 VID 的设备，接收器可能未插好)")
        for vid, pid, up, u, iface, name, path_s in sorted(found, key=lambda x: (x[0], x[1], x[2])):
            marker = ""
            for cfg in configs:
                try:
                    if (int(str(cfg.get("vendor_id", "0")), 16) == vid
                            and int(str(cfg.get("usage_page", "0")), 16) == up
                            and int(str(cfg.get("usage", "0")), 16) == u):
                        marker = f"  <== 命中 {cfg.get('id')}"
                        break
                except Exception:
                    pass
            out(lines, f"  {vid:04X}:{pid:04X}  UP={up:04X} U={u:04X} IF={iface}  {name!r}{marker}")
            out(lines, f"      {path_s[-95:]}")
    except Exception as e:
        out(lines, f"  HID 枚举失败: {e}")
    out(lines)

    # ── 最近缓存读数 ──
    out(lines, "[ 最后已知电量（缓存，不代表设备当前在线）]")
    cache_path = os.path.join(DATA_DIR, "battery_cache.json")
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            cache = json.load(f)
        for k, v in cache.items():
            out(lines, f"  {k}: {v}%")
    except Exception as e:
        out(lines, f"  读取缓存失败: {e}")
    out(lines)

    # ── 最近日志 ──
    out(lines, "[ 最近 60 行运行日志 ]")
    log_path = os.path.join(DATA_DIR, "volt.log")
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-60:]
        for line in tail:
            out(lines, "  " + line.rstrip())
    except Exception as e:
        out(lines, f"  读取日志失败: {e}")
    out(lines)
    out(lines, "=" * 68)
    out(lines, "本报告仅保存在本机，未上传任何数据。")
    out(lines, "贴到 GitHub issue 前请自行检查是否含有你不想公开的内容。")
    out(lines, "=" * 68)

    os.makedirs(DATA_DIR, exist_ok=True)
    target = os.path.join(DATA_DIR, f"diagnostics-{time.strftime('%Y%m%d-%H%M%S')}.txt")
    with open(target, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"诊断报告已生成: {target}")
    return target


if __name__ == "__main__":
    main()
