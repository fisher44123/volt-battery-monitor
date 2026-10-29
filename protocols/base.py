from abc import ABC
from typing import Optional, Dict, Any
from core.battery import BatteryStatus

class BaseProtocol(ABC):
    """
    所有外设通信协议解析器的统一基类。

    工作模型（对齐 rapoo-tray 的 HidWorkerThread）：
      1. 引擎在总线上找到“供应商专有 HID 集合”的设备路径；
      2. 打开设备后调用一次 on_open()，协议可在此发送唤醒/握手/查询命令；
      3. 监听线程持续阻塞读取，每收到一个报告就调用 parse_packet()；
      4. 协议在 parse_packet 中解析并缓存最新状态，返回 BatteryStatus。
    """
    protocol_id: str = "base"
    display_name: str = "Base Protocol"

    def read_length(self, device_config: Dict[str, Any]) -> int:
        """单次读取缓冲区长度（Windows 下建议 >= 报文最大长度）"""
        try:
            return int(device_config.get("read_length", 65))
        except Exception:
            return 65

    def on_open(self, hid_dev, device_config: Dict[str, Any]):
        """设备打开成功后调用一次，可发送唤醒/握手/查询命令"""
        pass

    # ── Feature Report 轮询（如 Compx 方案的电量在 Feature Report 上）──

    def poll_interval(self, device_config: Dict[str, Any]) -> int:
        """Feature Report 轮询间隔秒数，0 表示无需轮询（只被动监听）"""
        try:
            return int(device_config.get("poll_interval", 0))
        except Exception:
            return 0

    def poll_battery(self, hid_dev, device_config: Dict[str, Any]) -> Optional[BatteryStatus]:
        """周期性读取 Feature Report 解析电量；无需轮询时返回 None"""
        return None

    def parse_packet(self, data, device_config: Dict[str, Any]) -> Optional[BatteryStatus]:
        """
        持久监听线程每收到一个输入报告都会喂给本方法。
        :param data: hidapi read() 返回的字节列表（Windows 首字节为 Report ID）
        :return: 成功解析返回 BatteryStatus，否则 None
        """
        return None

    def read_battery(self, hid_dev, device_config: Dict[str, Any]) -> Optional[BatteryStatus]:
        """非持久（按需打开/读取一次）模式下的兼容入口"""
        return None

    def close(self):
        """清理资源"""
        pass
