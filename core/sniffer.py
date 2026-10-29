import hid
import time
from typing import List, Dict, Any, Optional

class HidSniffer:
    """HID 抓包与嗅探分析工具，用于探查新接入外设的 VID/PID 与报文特征"""

    @staticmethod
    def scan_all_devices() -> List[Dict[str, Any]]:
        """扫描当前系统所有 HID 设备端点"""
        results = []
        try:
            raw_devices = hid.enumerate()
        except Exception as e:
            print(f"[Sniffer] Enumerate failed: {e}")
            return []

        for d in raw_devices:
            vid = d.get('vendor_id', 0)
            pid = d.get('product_id', 0)
            up = d.get('usage_page', 0)
            u = d.get('usage', 0)
            if_num = d.get('interface_number', -1)
            path = d.get('path', b'')
            path_str = path.decode('utf-8', errors='replace') if isinstance(path, bytes) else str(path)
            
            prod = d.get('product_string') or "未知设备"
            mfg = d.get('manufacturer_string') or ""

            # 标记是否为常见供应商自定义通道 (UsagePage >= 0xFF00)
            is_vendor_channel = (up >= 0xFF00)

            results.append({
                "vendor_id": f"0x{vid:04X}",
                "product_id": f"0x{pid:04X}",
                "vid_int": vid,
                "pid_int": pid,
                "usage_page": f"0x{up:04X}",
                "usage": f"0x{u:04X}",
                "up_int": up,
                "u_int": u,
                "interface": if_num,
                "product": prod,
                "manufacturer": mfg,
                "path": path_str,
                "raw_path": path,
                "is_vendor": is_vendor_channel
            })
        return results

    @staticmethod
    def probe_endpoint(path: Any, send_hex: Optional[List[int]] = None, timeout_ms: int = 500) -> Dict[str, Any]:
        """对指定端点执行握手与捕获测试"""
        res = {
            "success": False,
            "written_bytes": 0,
            "read_bytes": 0,
            "data_hex": [],
            "error": None
        }

        try:
            h = hid.device()
            h.open_path(path if isinstance(path, bytes) else path.encode('utf-8'))
            h.set_nonblocking(False)

            if send_hex and len(send_hex) > 0:
                try:
                    w = h.write(send_hex)
                    res["written_bytes"] = w
                except Exception as we:
                    res["error"] = f"Write failed: {we}"

            data = h.read(65, timeout_ms=timeout_ms)
            if data:
                res["success"] = True
                res["read_bytes"] = len(data)
                res["data_hex"] = [f"0x{b:02X}" for b in data]
                res["data_raw"] = list(data)

            h.close()
        except Exception as e:
            res["error"] = str(e)

        return res

    # ═══════════════ 一键添加外设：被动捕获 + 字节偏移推断 ═══════════════

    # 已知协议查询指令库：被动零报文时自动轮询尝试（复用成熟协议帧头）
    KNOWN_QUERY_COMMANDS = [
        {"name": "compx(凌豹K98同款)", "cmd": [0x13, 0x05, 0x01, 0x00, 0x0A, 0x03], "pad": 64},
        {"name": "yjx(迈从G3V2同款)",  "cmd": [0x11, 0xF4, 0x55],                  "pad": 0},
        {"name": "rapoo(VT系列)",      "cmd": [0x06, 0x08, 0x00, 0xAA, 0x07],      "pad": 0},
    ]

    @staticmethod
    def capture_stream(path: Any, duration_s: float = 30.0,
                       should_stop: Optional[Any] = None,
                       progress_cb: Optional[Any] = None) -> Dict[str, Any]:
        """被动持续捕获指定端点的输入报文（后台线程调用）

        should_stop: callable() -> bool，外部取消标志
        progress_cb: (elapsed_s, frames_count) 进度回调
        返回: {"frames": [{"t": 秒, "raw": [int]}, ...], "cancelled": bool, "error": str|None}
        """
        out: Dict[str, Any] = {"frames": [], "cancelled": False, "error": None}
        h = None
        try:
            h = hid.device()
            h.open_path(path if isinstance(path, bytes) else path.encode('utf-8'))
            h.set_nonblocking(True)  # 非阻塞轮询，便于响应取消与进度
        except Exception as e:
            out["error"] = f"打开端点失败: {e}"
            return out

        import time as _t
        start = _t.time()
        try:
            while (_t.time() - start) < duration_s:
                if should_stop and should_stop():
                    out["cancelled"] = True
                    break
                data = h.read(65, timeout_ms=50)
                if data:
                    out["frames"].append({"t": round(_t.time() - start, 2), "raw": list(data)})
                if progress_cb:
                    try:
                        progress_cb(round(_t.time() - start, 1), len(out["frames"]))
                    except Exception:
                        pass
        except Exception as e:
            out["error"] = str(e)
        finally:
            try:
                h.close()
            except Exception:
                pass
        return out

    @staticmethod
    def query_probe(path: Any) -> Dict[str, Any]:
        """被动零报文时的兜底：自动尝试已知协议查询指令，收集响应"""
        result: Dict[str, Any] = {"frames": [], "tried": []}
        for q in HidSniffer.KNOWN_QUERY_COMMANDS:
            cmd = list(q["cmd"])
            if q.get("pad"):
                cmd = cmd + [0] * (q["pad"] - len(cmd))
            r = HidSniffer.probe_endpoint(path, send_hex=cmd, timeout_ms=400)
            result["tried"].append({"name": q["name"], "written": r.get("written_bytes", 0),
                                    "got": r.get("read_bytes", 0)})
            if r.get("success") and r.get("data_raw"):
                result["frames"].append({"t": 0.0, "raw": r["data_raw"], "via": q["name"]})
        return result

    @staticmethod
    def infer_battery_offset(frames: List[Dict[str, Any]], reported_pct: int) -> Dict[str, Any]:
        """字节偏移自动推断

        策略：遍历每个字节位，找"捕获期内出现过与用户申报电量相等数值"的偏移。
        辅助评分：该位置数值需满足 0<=v<=100；变化次数少（电量不乱跳）加分。
        返回: {"candidates": [{"offset", "score", "hits", "sample"}...], "best": {...}|None}
        """
        if not frames:
            return {"candidates": [], "best": None}

        max_len = min(64, max(len(f["raw"]) for f in frames))
        stats = []
        for off in range(max_len):
            vals = [f["raw"][off] for f in frames if off < len(f["raw"])]
            if not vals:
                continue
            distinct = sorted(set(vals))
            hits = sum(1 for v in vals if v == reported_pct)
            in_range = all(0 <= v <= 100 for v in vals)
            # 评分：命中申报值是硬信号；范围合法是软信号；稳定值加分
            score = hits * 10
            if in_range:
                score += 2
            if len(distinct) == 1 and in_range:
                score += 3  # 恒定合理值（大电池场景）
            if distinct == [0] or distinct == [0xFF]:
                score = -1  # 常量 0/FF 大概率不是电量
            if hits > 0:
                stats.append({"offset": off, "score": score, "hits": hits,
                              "distinct": len(distinct), "sample": distinct[:5]})

        stats.sort(key=lambda s: s["score"], reverse=True)
        candidates = [s for s in stats if s["score"] > 0][:5]
        return {"candidates": candidates, "best": candidates[0] if candidates else None}
