"""
凌豹 K98 (Compx 2.4G 接收器方案) 真实协议解析器

实测协议（抓包 + 蓝牙真值校验确认，VID=0x3554 / PID=0xFA09）：
  电量通道：Col01（UsagePage=0xFF02 / Usage=0x0002）。
  FR06 (Col06/0xFF04) 的 `06 00 02 64 64 64 9B 59 4B` 是恒定硬件信息，
  不是电量（蓝牙真值为 68% 时它仍返回 100），必须忽略。

  电量帧（dongle 推送的输入报告，Windows 蓝牙显示 68% 时实测 byte[6]=0x44）：
      13 0A 01 00 04 05 <pct> 01 00 ... <sum>
      byte[5]=0x05 表示电量上报，byte[6] = 电量百分比。
      校验和 = 前 19 字节之和（如 0x13+0x0A+0x01+0x00+0x04+0x05+0x44+0x01 = 0x6C）。

  按需查询（实测有效）：向 Col01 写输出报告 `13 05 01 00 0A 03` + 填充，
  dongle 立即推送上述电量帧。电量帧也会在键盘模式切换/重新握手时自动推送。
"""
import os
import json
import time
from typing import Optional, Dict, Any, Tuple
from core.battery import BatteryStatus
from protocols.base import BaseProtocol
from config import DATA_DIR

# 缓存文件必须用 DATA_DIR（打包后=dist/data，可写），不能用 __file__ 推导
# （打包后 __file__ 指向只读的 _MEIPASS 临时目录，缓存会永久丢失）
CACHE_FILE = os.path.join(DATA_DIR, "battery_cache.json")

def _load_cache(dev_id: str) -> int:
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
                return int(d.get(dev_id, -1))
    except Exception:
        pass
    return -1

def _save_cache(dev_id: str, pct: int):
    """原子写：先写 .tmp 再 replace，避免断电/强杀时把缓存文件写成半截垃圾"""
    try:
        d = {}
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    d = json.load(f)
            except Exception:
                d = {}
        if d.get(dev_id) == pct:
            return  # 值没变就不落盘，省掉无意义的磁盘写入
        d[dev_id] = pct
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)
    except Exception:
        pass

# 按需查询电量：写该输出报告后 dongle 会推送电量帧
BATTERY_QUERY = [0x13, 0x05, 0x01, 0x00, 0x0A, 0x03]


class CompxProtocol(BaseProtocol):
    protocol_id = "compx"
    display_name = "Compx 2.4G 协议"

    def __init__(self):
        super().__init__()
        self._last_battery = _load_cache("lingbao_k98")
        self._last_charging = False
        self._last_time = time.time() if self._last_battery > 0 else 0.0

    def _parse(self, data, dev_id: str = "lingbao_k98") -> Optional[Tuple[int, bool]]:
        """
        解析 Col01 输入报告中的电量帧：
            13 0A 01 00 04 05 <pct> 01 00 ... <sum>
        状态帧（byte[5]=0x02，byte[6]=0/1）不是电量，直接忽略。
        """
        if not data or len(data) < 7:
            return None
        if (data[0] == 0x13 and data[1] == 0x0A
                and data[4] == 0x04 and data[5] == 0x05):
            pct = data[6]
            if 1 <= pct <= 100:
                self._last_battery = pct
                self._last_charging = False
                self._last_time = time.time()
                _save_cache(dev_id, pct)
                return pct, False
        return None

    def poll_battery(self, hid_dev, device_config: Dict[str, Any]) -> Optional[BatteryStatus]:
        """按需查询电量：写查询命令 → 读 dongle 推送的电量帧"""
        if hid_dev is None:
            return self._build_from_cache(device_config)
        try:
            query = BATTERY_QUERY + [0] * (64 - len(BATTERY_QUERY))
            hid_dev.write(query)
            deadline = time.time() + 2.0
            while time.time() < deadline:
                buf = hid_dev.read(64, timeout_ms=300)
                if buf:
                    st = self.parse_packet(buf, device_config)
                    if st:
                        return st
        except Exception:
            pass
        return self._build_from_cache(device_config)

    def _build_from_cache(self, device_config: Dict[str, Any]) -> Optional[BatteryStatus]:
        """查询失败时的最后已知值。

        注意 raw_info 必须带 `(Cached)` —— engine 靠这个标记区分「真实硬件读数」
        和「上一次的旧值」。旧实现漏了这个标记，导致缓存值被当成实时真值，
        既违反「禁止虚构数值」约束，又让引擎误判为已同步而停止快速重试。
        """
        if self._last_battery > 0:
            return BatteryStatus(
                device_id=device_config.get("id", "lingbao_k98"),
                device_name=device_config.get("name", "凌豹 K98 机械键盘"),
                device_type=device_config.get("device_type", "keyboard"),
                percent=self._last_battery,
                charging=self._last_charging,
                online=True,
                connection_mode="2.4G",
                timestamp=self._last_time,
                raw_info=f"RealLingbaoPct={self._last_battery}%(Cached)"
            )
        return None

    def parse_packet(self, data, device_config: Dict[str, Any]) -> Optional[BatteryStatus]:
        dev_id = device_config.get("id", "lingbao_k98")
        res = self._parse(data, dev_id)
        if not res:
            return None
        pct, chg = res
        return BatteryStatus(
            device_id=device_config.get("id", "lingbao_k98"),
            device_name=device_config.get("name", "凌豹 K98 机械键盘"),
            device_type=device_config.get("device_type", "keyboard"),
            percent=pct,
            charging=chg,
            online=True,
            connection_mode="2.4G",
            timestamp=self._last_time,
            raw_info=f"RealLingbaoPct={pct}%"
        )

    def on_open(self, hid_dev, device_config: Dict[str, Any]):
        """打开后立即按需查询一次电量"""
        try:
            self.poll_battery(hid_dev, device_config)
        except Exception:
            pass

    def read_battery(self, hid_dev, device_config: Dict[str, Any]) -> Optional[BatteryStatus]:
        """兼容入口：按需查询电量"""
        dev_id = device_config.get("id", "lingbao_k98")
        dev_name = device_config.get("name", "凌豹 K98 机械键盘")
        if hid_dev:
            st = self.poll_battery(hid_dev, device_config)
            if st:
                return st

        if self._last_battery > 0:
            return BatteryStatus(
                device_id=dev_id,
                device_name=dev_name,
                device_type="keyboard",
                percent=self._last_battery,
                charging=self._last_charging,
                online=True,
                connection_mode="2.4G",
                timestamp=self._last_time,
                raw_info=f"RealLingbaoPct={self._last_battery}%(Cached)"
            )

        return BatteryStatus(
            device_id=dev_id,
            device_name=dev_name,
            device_type="keyboard",
            percent=-1,
            charging=False,
            online=True,
            connection_mode="2.4G",
            timestamp=time.time(),
            raw_info="WaitingForFirstPacket"
        )
