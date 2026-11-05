"""
校验 devices/*.json，并可由同一份数据生成兼容表。

借鉴 myfinder-device-battery 的 device_pack_schema.json + catalog.py 思路：
配置是唯一真源，文档自动生成，避免文档与代码脱节。

用法：
    python scripts/validate_devices.py                # 只校验，出错退出码 1
    python scripts/validate_devices.py --write-docs   # 校验通过后生成 docs/compatibility.md
"""
import argparse
import json
import os
import re
import sys

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEVICES_DIR = os.path.join(PROJ, "devices")
DOC_PATH = os.path.join(PROJ, "docs", "compatibility.md")

HEX_ID = re.compile(r"^0x[0-9A-Fa-f]{4}$")
KINDS = {"mouse", "keyboard", "headset", "gamepad", "device"}
LEVELS = {"A", "B", "C", "D", "E"}
REQUIRED = ("id", "name", "device_type", "vendor_id", "product_ids",
            "usage_page", "usage", "protocol", "enabled")

LEVEL_MEANING = {
    "A": "本项目实机读数验证",
    "B": "实机验证，但覆盖条件有限",
    "C": "协议已接入，待实机验证",
    "D": "已发现但未取得读数，不宣称兼容",
    "E": "已排除",
}


def check_one(path):
    """返回 (config, errors)"""
    errors = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        return None, [f"{os.path.basename(path)}: JSON 解析失败: {e}"]

    fname = os.path.basename(path)
    for key in REQUIRED:
        if key not in cfg:
            errors.append(f"{fname}: 缺少必填字段 {key}")

    if "id" in cfg and cfg["id"] != os.path.splitext(fname)[0]:
        errors.append(f"{fname}: id 与文件名不一致 ({cfg['id']})")

    for key in ("vendor_id", "usage_page", "usage"):
        if key in cfg and not (isinstance(cfg[key], str) and HEX_ID.match(cfg[key])):
            errors.append(f"{fname}: {key} 必须是 0x 开头的 4 位十六进制，如 0x3554")

    if "product_ids" in cfg:
        pids = cfg["product_ids"]
        if not isinstance(pids, list) or not pids:
            errors.append(f"{fname}: product_ids 必须是非空数组")
        else:
            for pid in pids:
                if not (isinstance(pid, str) and HEX_ID.match(pid)):
                    errors.append(f"{fname}: product_ids 含非法值 {pid!r}")

    if "device_type" in cfg and cfg["device_type"] not in KINDS:
        errors.append(f"{fname}: device_type 必须是 {sorted(KINDS)} 之一")
    if "enabled" in cfg and not isinstance(cfg["enabled"], bool):
        errors.append(f"{fname}: enabled 必须是 true/false")
    if "product_names" in cfg and (
            not isinstance(cfg["product_names"], list)
            or not all(isinstance(n, str) for n in cfg["product_names"])):
        errors.append(f"{fname}: product_names 必须是字符串数组")
    if "require_exact_usage" in cfg and not isinstance(cfg["require_exact_usage"], bool):
        errors.append(f"{fname}: require_exact_usage 必须是 true/false")
    if "evidence_level" in cfg and cfg["evidence_level"] not in LEVELS:
        errors.append(f"{fname}: evidence_level 必须是 {sorted(LEVELS)} 之一")
    if "protocol" in cfg and (not isinstance(cfg["protocol"], str) or not cfg["protocol"].strip()):
        errors.append(f"{fname}: protocol 不能为空")

    return cfg, errors


def collect():
    configs, all_errors = [], []
    for fname in sorted(os.listdir(DEVICES_DIR)):
        if not fname.endswith(".json") or fname == "template.json":
            continue
        cfg, errs = check_one(os.path.join(DEVICES_DIR, fname))
        all_errors.extend(errs)
        if cfg:
            configs.append(cfg)

    # 同一 (VID, PID, 集合) 不能被两份配置争抢
    seen = {}
    for cfg in configs:
        for pid in cfg.get("product_ids", []):
            key = (cfg.get("vendor_id"), pid,
                   cfg.get("usage_page"), cfg.get("usage"))
            if key in seen:
                all_errors.append(f"{cfg.get('id')} 与 {seen[key]} 争抢同一身份 {key}")
            seen[key] = cfg.get("id")
    return configs, all_errors


def write_docs(configs):
    lines = [
        "<!-- 由 `python scripts/validate_devices.py --write-docs` 自动生成，勿手改 -->",
        "# 设备兼容表",
        "",
        "唯一数据来源：`devices/*.json`。等级说明：",
        "",
    ]
    for lv in ("A", "B", "C", "D", "E"):
        lines.append(f"- **{lv}** = {LEVEL_MEANING[lv]}")
    lines += [
        "",
        "等级只说明证据范围，不代表同品牌全系列可用。同一 VID:PID 可能被多家厂商的接收器复用，",
        "本项目按 VID/PID + UsagePage/Usage + 产品名三重精确匹配，身份不符时不发送任何厂商查询命令。",
        "",
        "| 设备 | 类型 | VID:PID | 集合 | 协议 | 等级 | 边界与证据 |",
        "|---|---|---|---|---|---|---|",
    ]
    for cfg in configs:
        pids = ", ".join(cfg.get("product_ids", []))
        level = cfg.get("evidence_level", "未标注")
        note = cfg.get("evidence_note") or cfg.get("description", "")
        note = str(note).replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {cfg.get('name', cfg.get('id'))} | {cfg.get('device_type', '')} | "
            f"{cfg.get('vendor_id', '')} / {pids} | "
            f"{cfg.get('usage_page', '')}:{cfg.get('usage', '')} | "
            f"{cfg.get('protocol', '')} | {level} | {note} |"
        )
    lines += [
        "",
        "添加新设备：`cp devices/template.json devices/<型号>.json`，填好后运行",
        "`python scripts/validate_devices.py --write-docs` 校验并刷新本表。",
        "",
    ]
    os.makedirs(os.path.dirname(DOC_PATH), exist_ok=True)
    with open(DOC_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"兼容表已生成: {DOC_PATH}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write-docs", action="store_true", help="校验通过后生成兼容表")
    args = ap.parse_args()

    configs, errors = collect()
    for e in errors:
        print("ERROR:", e)
    if errors:
        print(f"\n校验失败：{len(errors)} 个问题")
        return 1
    print(f"校验通过：{len(configs)} 份设备配置")
    if args.write_docs:
        write_docs(configs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
