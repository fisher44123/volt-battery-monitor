import time
from typing import Optional, Dict, Any
from core.battery import BatteryStatus
from protocols.base import BaseProtocol

class StandardHidProtocol(BaseProtocol):
    """
    通用 / 声明式可配置 HID 电量解析器
    允许在 JSON 中自由指定：
    - query_command (可选发送的十六进制数组)
    - report_id (期待接收的报告 ID)
    - battery_byte_offset (电量所在字节位置)
    - charging_byte_offset / charging_bit_mask (充电位位置及掩码)
    - xor_mask (可选的异或掩码，如 0xFF)
    """
    protocol_id = "standard_hid"
    display_name = "通用声明式 HID 协议"

    def _build_query(self, custom: Dict[str, Any]):
        query_hex = custom.get("query_hex")
        if query_hex:
            return [int(x, 16) if isinstance(x, str) else int(x) for x in query_hex]
        return None

    def on_open(self, hid_dev, device_config: Dict[str, Any]):
        custom = device_config.get("protocol_custom", {})
        cmd = self._build_query(custom)
        if cmd:
            try:
                hid_dev.write(cmd)
            except Exception:
                pass

    def parse_packet(self, data, device_config: Dict[str, Any]) -> Optional[BatteryStatus]:
        dev_id = device_config.get("id", "generic_hid")
        dev_name = device_config.get("name", "通用 HID 设备")
        dev_type = device_config.get("device_type", "mouse")
        custom = device_config.get("protocol_custom", {})

        if not data:
            return None

        # 1. 异或解密 (若配置)
        xor_mask = custom.get("xor_mask", 0)
        if xor_mask:
            data = [b ^ xor_mask for b in data]

        # 2. 匹配期待的 Report ID (若指定)
        expected_rid = custom.get("expected_report_id")
        if expected_rid is not None:
            if len(data) == 0 or data[0] != expected_rid:
                return None

        # 3. 读取电量字节
        bat_offset = custom.get("battery_byte_offset", 4)
        if len(data) > bat_offset:
            pct = data[bat_offset]
            if 0 <= pct <= 100:
                # 判断充电
                charging = False
                chg_offset = custom.get("charging_byte_offset")
                if chg_offset is not None and len(data) > chg_offset:
                    mask = custom.get("charging_bit_mask", 0x01)
                    charging = bool(data[chg_offset] & mask)

                return BatteryStatus(
                    device_id=dev_id,
                    device_name=dev_name,
                    device_type=dev_type,
                    percent=pct,
                    charging=charging,
                    online=True,
                    connection_mode=device_config.get("connection_mode", "2.4G"),
                    timestamp=time.time()
                )

        return None

    def read_battery(self, hid_dev, device_config: Dict[str, Any]) -> Optional[BatteryStatus]:
        custom = device_config.get("protocol_custom", {})

        # 1. 发送可选的查询命令
        if hid_dev:
            self.on_open(hid_dev, device_config)

            # 2. 读取输入报告
            data = None
            for _ in range(3):
                try:
                    buf = hid_dev.read(custom.get("read_length", 64), timeout_ms=150)
                except Exception:
                    buf = None
                if buf:
                    data = buf
                    break

            if data:
                return self.parse_packet(data, device_config)

        return None
