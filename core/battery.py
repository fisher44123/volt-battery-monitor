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

    # 超过这个秒数的读数视为"历史读数"：界面灰显、不触发低电量提醒。
    # 与 myfinder-device-battery 的 90 秒新鲜度口径一致。
    FRESHNESS_SEC = 90

    @property
    def is_valid(self) -> bool:
        return self.online and (0 <= self.percent <= 100)

    @property
    def is_cached(self) -> bool:
        """最后已知值，不是这一轮硬件回包"""
        return "Cached" in (self.raw_info or "")

    @property
    def is_fresh(self) -> bool:
        return self.timestamp > 0 and (time.time() - self.timestamp) <= self.FRESHNESS_SEC

    @property
    def is_trustworthy(self) -> bool:
        """当前真实读数：在线 + 非缓存 + 未过期。
        只有可信读数才用彩色电量、才允许触发低电量提醒。"""
        return self.is_valid and not self.is_cached and self.is_fresh

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
