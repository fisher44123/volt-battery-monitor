from dataclasses import dataclass, field
import time
from typing import Optional, List

@dataclass
class BatteryStatus:
    device_id: str                      # 唯一标识符（例如 mchose_g3v2）
    device_name: str                    # 展示名称（例如 迈从 G3 V2）
    device_type: str                    # "mouse" 或 "keyboard"
    percent: int = -1                   # 电量 0~100，-1 表示未获取或离线
    charging: bool = False              # 是否处于充电中
    online: bool = False                # 设备是否在线（通过 2.4G 接收器响应）
    connection_mode: str = "2.4G"       # "2.4G", "USB", "Unknown"
    timestamp: float = field(default_factory=time.time) # 状态更新时间戳
    raw_info: Optional[str] = None      # 原始通信附带信息

    @property
    def is_valid(self) -> bool:
        return self.online and (0 <= self.percent <= 100)

    @classmethod
    def offline(cls, device_id: str, name: str, dev_type: str = "mouse"):
        return cls(
            device_id=device_id,
            device_name=name,
            device_type=dev_type,
            percent=-1,
            charging=False,
            online=False,
            connection_mode="Unknown"
        )
