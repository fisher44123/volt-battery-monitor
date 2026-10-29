import time
from typing import Optional, Dict, Any
from core.battery import BatteryStatus
from protocols.base import BaseProtocol

class RapooProtocol(BaseProtocol):
    """
    雷柏 (RAPOO) VT 系列无线鼠标专有协议
    严格对齐 Iris-0109/rapoo-tray 源码逆向结论（注意：与 README 的简化描述有出入，
    一切以实际 C++ 源码行为为准）：

    1. 通信通道
       - VID=0x24AE；供应商专有通道是设备的第 9 个 HID 顶层集合
         （Windows 设备路径中含 “col09”，UsagePage 0xFF00 / Usage 0x0002）。
       - 普通鼠标集合（col01）上报移动/按键输入报告，不含电量；
         0x07 状态包只在 col09 上出现。
       - 主机以 GENERIC_READ 只读方式打开，全程不发送任何命令，
         设备以中断传输主动、持续推送状态包。

    2. Report ID 0x07，19 字节状态数据包：
       Byte [0]    : 0x07
       Byte [1]    : 设备标识（接收器模式为 0x20）
       Byte [2]    : DPI 档位（从 0 开始，0 表示第 1 档）
       Byte [3..4] : X 轴 DPI，16 位小端（如 0x04B0 = 1200）
       Byte [5..6] : Y 轴 DPI，16 位小端
       Byte [7]    : 活跃状态 0x01 通信中 / 0x00 休眠
       Byte [8]    : 物理电量百分比 0x00 ~ 0x64
    """
    protocol_id = "rapoo"
    display_name = "雷柏 VT 系列协议"

    def __init__(self):
        self._last_battery = -1
        self._last_active = False
        self._last_dpi_x = -1
        self._last_dpi_y = -1
        self._last_dpi_level = -1
        self._last_time = 0.0

    @staticmethod
    def _align_to_report(data) -> Optional[list]:
        """
        hidapi 在 Windows 下 read() 返回的首字节即 Report ID。
        兼容个别接口存在前导对齐字节的情况（此时 0x07 出现在 [1]）。
        """
        if not data or len(data) < 9:
            return None
        if data[0] == 0x07:
            return data
        if len(data) > 9 and data[1] == 0x07:
            return data[1:]
        return None

    def parse_packet(self, data, device_config: Dict[str, Any]) -> Optional[BatteryStatus]:
        raw = self._align_to_report(data)
        if raw is None:
            return None

        dpi_level = raw[2] + 1
        dpi_x = raw[3] | (raw[4] << 8)
        dpi_y = raw[5] | (raw[6] << 8)
        active = (raw[7] == 0x01)
        bat = max(0, min(100, raw[8]))

        self._last_battery = bat
        self._last_active = active
        self._last_dpi_x = dpi_x
        self._last_dpi_y = dpi_y
        self._last_dpi_level = dpi_level
        self._last_time = time.time()

        return self._build_status(device_config)

    def _build_status(self, device_config: Dict[str, Any]) -> Optional[BatteryStatus]:
        if self._last_battery < 0:
            return None
        dpi_x = self._last_dpi_x if self._last_dpi_x >= 0 else 0
        return BatteryStatus(
            device_id=device_config.get("id", "rapoo_mouse"),
            device_name=device_config.get("name", "雷柏鼠标"),
            device_type=device_config.get("device_type", "mouse"),
            percent=self._last_battery,
            charging=False,
            online=True,
            connection_mode="2.4G",
            timestamp=self._last_time,
            raw_info=f"DPI={dpi_x} (第 {self._last_dpi_level} 档)"
        )

    def read_battery(self, hid_dev, device_config: Dict[str, Any]) -> Optional[BatteryStatus]:
        """兼容入口：若给了设备句柄就主动读若干次，否则返回缓存状态。"""
        if hid_dev is not None:
            for _ in range(8):
                try:
                    buf = hid_dev.read(self.read_length(device_config), timeout_ms=100)
                except Exception:
                    break
                if buf:
                    st = self.parse_packet(buf, device_config)
                    if st:
                        return st
        return self._build_status(device_config)
