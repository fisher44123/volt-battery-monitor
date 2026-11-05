import os
import time
import json
import threading
import hid
from typing import Dict, List, Optional, Callable, Any, Tuple
from core.battery import BatteryStatus
from core.stats import stats_manager
from protocols.registry import protocol_registry
from config import DEVICES_DIR, RESOURCE_DIR, DATA_DIR

def _to_int(value) -> int:
    if isinstance(value, str):
        return int(value, 16)
    return int(value)

def _path_to_str(path) -> str:
    if isinstance(path, bytes):
        # Windows HID 路径为 ASCII（\\?\hid#...）
        return path.decode("latin1", errors="ignore")
    return str(path)


# 设备一动就补读一次电量的最小间隔（秒）
ACTIVITY_RECHECK_SEC = 8


def _is_reportable(st: Optional[BatteryStatus]) -> bool:
    """这条状态值不值得推给界面。

    旧实现只认 `percent > 0`，于是鼠标休眠时返回的
    「pct=0 + raw_info=Sleeping」帧被整条丢弃 —— 开机时若鼠标已在休眠，
    界面就永远卡在「待同步」，用户以为程序坏了。
    """
    if st is None:
        return False
    if st.percent > 0:
        return True
    return getattr(st, "raw_info", "") == "Sleeping"

class DeviceListener:
    """
    单台设备的持久 HID 监听线程。
    对齐 rapoo-tray 的 HidWorkerThread：打开供应商专有集合 -> on_open 握手
    -> 持续阻塞读取 -> parse_packet 解析，设备句柄全程保持不关闭。
    """

    def __init__(self, cfg: Dict[str, Any], protocol,
                 status_callback: Callable[[str, Optional[BatteryStatus]], None],
                 open_failed_callback: Optional[Callable[[str, str], None]] = None):
        self.cfg = cfg
        self.protocol = protocol
        self._status_callback = status_callback
        self._open_failed_callback = open_failed_callback
        self.path: Optional[bytes] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_open_err = ""

    @property
    def dev_id(self) -> str:
        return self.cfg.get("id", "unknown")

    def start(self, path) -> bool:
        self.path = path
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run,
                                        name=f"HidListener-{self.dev_id}", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop_event.set()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=1.0)
        self._thread = None
        self.path = None

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self):
        h = hid.device()
        try:
            h.open_path(self.path)
            h.set_nonblocking(False)
        except Exception as e:
            msg = str(e)
            # 同一错误只上报一次，避免 2 秒对账刷屏
            if msg != self._last_open_err:
                self._last_open_err = msg
                print(f"[Listener:{self.dev_id}] 打开设备失败: {msg}")
            if self._open_failed_callback:
                try:
                    self._open_failed_callback(self.dev_id, "InterfaceBusy")
                except Exception:
                    pass
            return
        self._last_open_err = ""

        try:
            try:
                self.protocol.on_open(h, self.cfg)
            except Exception as e:
                print(f"[Listener:{self.dev_id}] on_open 异常: {e}")

            base_poll_interval = self.protocol.poll_interval(self.cfg)
            last_poll = 0.0
            last_activity_poll = 0.0
            read_len = self.protocol.read_length(self.cfg)
            synced = False

            while not self._stop_event.is_set():
                if base_poll_interval > 0:
                    # 未同步到有效电量时每 3 秒快速重试探测，同步后恢复正常周期 (如 30 秒)
                    cur_interval = base_poll_interval if synced else 3.0
                    now = time.time()
                    if now - last_poll >= cur_interval:
                        last_poll = now
                        try:
                            st = self.protocol.poll_battery(h, self.cfg)
                        except Exception as e:
                            print(f"[Listener:{self.dev_id}] 轮询异常: {e}")
                            st = None
                        # Cached 标记（raw_info 含 "Cached"）表示非真实硬件读数：
                        # 不视为已同步，仅作 UI 占位展示，继续等待真实帧
                        is_cached = st is not None and "Cached" in (getattr(st, "raw_info", "") or "")
                        if st is not None and st.percent > 0 and not is_cached:
                            if not synced:
                                print(f"[Listener:{self.dev_id}] 首次同步成功 (poll): 电量={st.percent}% 充电={st.charging} 来源={getattr(st, 'raw_info', '')}")
                            synced = True
                            try:
                                self._status_callback(self.dev_id, st)
                            except Exception:
                                pass
                        elif st is not None and _is_reportable(st) and not synced:
                            # 缓存值 / 休眠帧仅展示，不标记同步（继续被动监听真实帧）
                            try:
                                self._status_callback(self.dev_id, st)
                            except Exception:
                                pass
                        continue

                    try:
                        buf = h.read(read_len, timeout_ms=150)
                    except Exception as e:
                        print(f"[Listener:{self.dev_id}] read 异常: {e}")
                        break
                    if not buf:
                        continue

                    # 活动补读：HID 输入报告只在设备真的有动作时到达，
                    # 所以收到任意报告就趁机查一次电量（8 秒节流，且不与定时
                    # 轮询撞车）。休眠醒来晃动鼠标时能立刻刷新，不必干等
                    # 15/30 秒的固定周期。吸收自 myfinder 的 USB 活动补读。
                    act_now = time.time()
                    if (synced
                            and act_now - last_activity_poll >= ACTIVITY_RECHECK_SEC
                            and act_now - last_poll >= 2.0):
                        last_activity_poll = act_now
                        last_poll = act_now
                        try:
                            act_st = self.protocol.poll_battery(h, self.cfg)
                        except Exception:
                            act_st = None
                        act_cached = (act_st is not None
                                      and "Cached" in (getattr(act_st, "raw_info", "") or ""))
                        if act_st is not None and act_st.percent > 0 and not act_cached:
                            print(f"[Listener:{self.dev_id}] 活动补读: {act_st.percent}%", flush=True)
                            try:
                                self._status_callback(self.dev_id, act_st)
                            except Exception:
                                pass

                    try:
                        st = self.protocol.parse_packet(buf, self.cfg)
                    except Exception as e:
                        st = None
                    if st is not None and st.percent > 0:
                        if not synced:
                            print(f"[Listener:{self.dev_id}] 首次同步成功 (packet): 电量={st.percent}% 充电={st.charging} 帧头={' '.join(f'{b:02X}' for b in buf[:8])}")
                        synced = True
                        try:
                            self._status_callback(self.dev_id, st)
                        except Exception:
                            pass
                    elif _is_reportable(st):
                        # 休眠帧：只推状态，不改变 synced（仍需继续等真实帧）
                        try:
                            self._status_callback(self.dev_id, st)
                        except Exception:
                            pass
                    continue

                try:
                    buf = h.read(read_len, timeout_ms=200)
                except Exception as e:
                    print(f"[Listener:{self.dev_id}] read 异常: {e}")
                    break
                if not buf:
                    continue
                try:
                    st = self.protocol.parse_packet(buf, self.cfg)
                except Exception as e:
                    print(f"[Listener:{self.dev_id}] parse 异常: {e}")
                    st = None
                if st is not None:
                    try:
                        self._status_callback(self.dev_id, st)
                    except Exception as e:
                        print(f"[Listener:{self.dev_id}] 状态回调异常: {e}")
        finally:
            try:
                h.close()
            except Exception:
                pass

class DeviceEngine:
    """
    外设发现 / 热插拔 / 持久监听引擎。

    旧实现存在三个致命问题（导致永远拿不到电量）：
      1. _persistent_listeners 只被读取、从不被创建，协议 read_battery
         根本没被调用，HID 设备从未被打开；
      2. _find_device_path 仅按 VID/PID 选中第一个接口，对雷柏而言命中的是
         col01 普通鼠标集合，而 0x07 状态包只出现在 col09 供应商集合；
      3. 仅靠 3 次 150ms 短读，无法接住设备异步推送的状态包。
    现改为：2 秒一次总线对账 + 每设备常驻监听线程。
    """

    def __init__(self, on_update_callback: Optional[Callable[[Dict[str, BatteryStatus]], None]] = None):
        self.on_update_callback = on_update_callback
        self.devices_config: Dict[str, Dict[str, Any]] = {}
        self.statuses: Dict[str, BatteryStatus] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self._listeners: Dict[str, DeviceListener] = {}

        # 缓存最后已知电量，防止无线设备休眠/重启程序后显示空白
        # 注意：必须用 DATA_DIR（打包后=dist/data），不能用 __file__ 推导
        # （打包后 __file__ 指向只读的 _MEIPASS 临时目录，缓存会永久丢失）
        self._cache_file = os.path.join(DATA_DIR, "battery_cache.json")
        self._last_known_battery: Dict[str, int] = self._load_cache()

        self.load_device_configs()

    # ──────────────────────── 持久化缓存 ────────────────────────

    def _load_cache(self) -> Dict[str, int]:
        if os.path.exists(self._cache_file):
            try:
                with open(self._cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_cache(self):
        """原子写缓存（tmp + replace），避免异常退出时把 JSON 写成半截"""
        try:
            os.makedirs(os.path.dirname(self._cache_file), exist_ok=True)
            tmp = self._cache_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._last_known_battery, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._cache_file)
        except Exception as e:
            print(f"[Engine] 写缓存失败: {e}")

    # ──────────────────────── 设备配置 ────────────────────────

    @staticmethod
    def _sync_builtin_configs():
        """把打包内置的官方设备配置同步到可写目录。

        旧逻辑只在「DEVICES_DIR 完全没有配置文件」时复制一次，结果重新打包后
        dist/devices 里还是上一版的 lingbao_k98.json，新增的身份校验字段
        永远不生效。现在每次启动按 id 对齐覆盖三份官方配置，用户自建的
        其它 JSON 一律不动；打包后 RESOURCE_DIR 是 _MEIPASS，
        开发模式下 src 与 DEVICES_DIR 是同一目录，直接跳过。
        """
        import shutil
        src = os.path.join(RESOURCE_DIR, "devices")
        if not os.path.isdir(src) or os.path.abspath(src) == os.path.abspath(DEVICES_DIR):
            return
        os.makedirs(DEVICES_DIR, exist_ok=True)
        for fname in ("lingbao_k98.json", "mchose_g3v2.json", "template.json"):
            s, d = os.path.join(src, fname), os.path.join(DEVICES_DIR, fname)
            if not os.path.exists(s):
                continue
            try:
                if os.path.exists(d):
                    with open(s, "r", encoding="utf-8") as f:
                        builtin = json.load(f)
                    with open(d, "r", encoding="utf-8") as f:
                        current = json.load(f)
                    if builtin == current:
                        continue
                    # 用户把这个文件改成自己的设备了 -> 不覆盖
                    if builtin.get("id") and current.get("id") != builtin.get("id"):
                        continue
                shutil.copy2(s, d)
                print(f"[Engine] 已同步内置设备配置 {fname}")
            except Exception as e:
                print(f"[Engine] 同步 {fname} 失败: {e}")

    def load_device_configs(self):
        """扫描 devices/ 目录，加载所有启用的外设配置。
        若 BASE_DIR/devices 为空则从 RESOURCE_DIR/devices 回退复制。"""
        self._sync_builtin_configs()
        with self._lock:
            self.devices_config.clear()

            # 若 DEVICES_DIR 不存在或无有效配置，尝试从 RESOURCE_DIR 复制
            has_valid_json = (os.path.isdir(DEVICES_DIR)
                              and any(f.endswith(".json") and f != "template.json"
                                      for f in os.listdir(DEVICES_DIR)))
            if not has_valid_json:
                _src = os.path.join(RESOURCE_DIR, "devices")
                print(f"[Engine] DEVICES_DIR 无有效配置，尝试从 {_src} 复制...")
                if os.path.isdir(_src):
                    try:
                        os.makedirs(DEVICES_DIR, exist_ok=True)
                        import shutil
                        _copied = 0
                        for f in os.listdir(_src):
                            if f.endswith(".json"):
                                _src_f = os.path.join(_src, f)
                                _dst_f = os.path.join(DEVICES_DIR, f)
                                try:
                                    shutil.copy2(_src_f, _dst_f)
                                    _copied += 1
                                    print(f"[Engine] 已复制 {f}")
                                except Exception as _e:
                                    print(f"[Engine] 复制 {f} 失败: {_e}")
                        print(f"[Engine] 从 RESOURCE_DIR 复制完成，共 {_copied} 个文件")
                    except Exception as _e:
                        print(f"[Engine] 回退复制整体失败: {_e}")
                else:
                    print(f"[Engine] RESOURCE_DIR/devices 也不存在，扫描完整 RESOURCE_DIR 找 json...")
                    # 最后兜底：扫描 RESOURCE_DIR 下所有含 *.json 的子目录
                    for _root, _dirs, _files in os.walk(RESOURCE_DIR):
                        _json_files = [f for f in _files if f.endswith(".json") and f != "template.json"]
                        if _json_files:
                            print(f"[Engine] 在 {_root} 找到 {len(_json_files)} 个 json")
                            os.makedirs(DEVICES_DIR, exist_ok=True)
                            import shutil
                            for f in _json_files:
                                try:
                                    shutil.copy2(os.path.join(_root, f), os.path.join(DEVICES_DIR, f))
                                    print(f"[Engine] 已复制 {f}")
                                except Exception as _e:
                                    print(f"[Engine] 复制 {f} 失败: {_e}")
                            break

            if not os.path.exists(DEVICES_DIR):
                print(f"[Engine] DEVICES_DIR={DEVICES_DIR} 不存在，无设备可加载")
                return

            for fname in os.listdir(DEVICES_DIR):
                if fname.endswith(".json") and fname != "template.json":
                    fpath = os.path.join(DEVICES_DIR, fname)
                    try:
                        with open(fpath, "r", encoding="utf-8") as f:
                            cfg = json.load(f)
                            if cfg.get("enabled", True):
                                dev_id = cfg.get("id", os.path.splitext(fname)[0])
                                cfg["id"] = dev_id
                                self.devices_config[dev_id] = cfg

                                # 初始占位一律 percent=-1：缓存值仅供统计，
                                # 不作为电量展示（虚假数据根因之一）
                                if dev_id not in self.statuses:
                                    self.statuses[dev_id] = BatteryStatus(
                                        device_id=dev_id,
                                        device_name=cfg.get("name", dev_id),
                                        device_type=cfg.get("device_type", "mouse"),
                                        percent=-1,
                                        charging=False,
                                        online=True,
                                        connection_mode="2.4G",
                                        timestamp=time.time(),
                                        raw_info="WaitingForHardwareSync"
                                    )
                    except Exception as e:
                        print(f"[Engine] 加载配置失败 {fname}: {e}")

            print(f"[Engine] 加载设备配置完成，共 {len(self.devices_config)} 台: {list(self.devices_config.keys())}")

    # ──────────────────────── 接口选择 ────────────────────────

    @staticmethod
    def _select_interface(cfg: Dict[str, Any],
                          bus_devices: List[Dict]) -> Tuple[Optional[bytes], int]:
        """
        在总线设备中选择供应商专有 HID 集合。

        身份校验（2026-11-05 加严）：
        同一 VID/PID 会被多家厂商的不同接收器复用。实测本机 3554:FA09
        同时挂着 FF02:0002（凌豹 Compx 电量）与 FF04:0002（另一套 MCHOSE
        键盘协议，20 字节 ID 13）；3837:4245 也同时有 FF01:0010、FF05/FF06
        等多个私有集合。旧实现只要 VID/PID 相同就给 1 分兜底选中，会把
        凌豹的查询命令发到身份不明的集合上。
        现在：配置了 usage_page+usage 时，默认只接受精确匹配（100 分），
        并且 product_names 白名单（若配置）必须命中，否则一律不选、不发命令。

        评分：UsagePage+Usage 精确匹配 = 100；
              路径含 collection_hint（如 col09）= 60；
              仅 VID/PID 匹配 = 1。
        :return: (device_path, score)
        """
        target_vid = _to_int(cfg.get("vendor_id", "0x0000"))
        pids_cfg = cfg.get("product_ids", [])
        target_pids = [_to_int(p) for p in pids_cfg]

        want_up: Optional[int] = None
        want_u: Optional[int] = None
        if cfg.get("usage_page"):
            want_up = int(str(cfg.get("usage_page")), 16)
        if cfg.get("usage"):
            want_u = int(str(cfg.get("usage")), 16)

        col_hint = cfg.get("collection_hint")  # 例如 "col09"
        allowed_names = [str(n).strip().lower()
                         for n in (cfg.get("product_names") or []) if str(n).strip()]
        # 配了 usage 就必须精确命中；想退回旧宽松行为须在设备 JSON 里显式关掉
        exact_required = (want_up is not None and want_u is not None
                          and bool(cfg.get("require_exact_usage", True)))

        best_path: Optional[bytes] = None
        best_score = -1
        rejected_name = 0
        rejected_usage = 0

        for d in bus_devices:
            if d.get("vendor_id") != target_vid:
                continue
            if target_pids and d.get("product_id") not in target_pids:
                continue

            if allowed_names:
                dev_name = str(d.get("product_string") or "").strip().lower()
                if dev_name not in allowed_names:
                    rejected_name += 1
                    continue

            score = 1
            if (want_up is not None and want_u is not None
                    and d.get("usage_page") == want_up and d.get("usage") == want_u):
                score = 100
            elif col_hint:
                low = _path_to_str(d.get("path", b"")).lower()
                if str(col_hint).lower() in low:
                    score = max(score, 60)

            if exact_required and score != 100:
                rejected_usage += 1
                continue

            if score > best_score:
                best_score = score
                best_path = d.get("path")

        if best_path is None and (rejected_name or rejected_usage):
            print(f"[Engine] {cfg.get('id', '?')} 身份校验未通过，拒绝发送任何厂商命令"
                  f"（产品名不符 {rejected_name} 项 / 集合不符 {rejected_usage} 项）")

        return best_path, best_score

    # ──────────────────────── 状态更新 ────────────────────────

    def _on_listener_status(self, dev_id: str, st: Optional[BatteryStatus]):
        """监听线程解析出有效状态时回调（在监听线程中执行）"""
        if st is None:
            return

        changed = False
        pct_changed = False
        new_pct = None
        with self._lock:
            curr = self.statuses.get(dev_id)
            if (curr is None or curr.percent != st.percent
                    or curr.charging != st.charging or not curr.online
                    or getattr(curr, "raw_info", "") != getattr(st, "raw_info", "")):
                changed = True
            self.statuses[dev_id] = st
            if 0 <= st.percent <= 100:
                new_pct = st.percent
                if self._last_known_battery.get(dev_id) != new_pct:
                    self._last_known_battery[dev_id] = new_pct
                    pct_changed = True
                    changed = True

        if new_pct is not None:
            # 只在电量真的变了才落盘：旧实现每次回调都写一次 JSON，
            # 两台设备合计每天约 8600 次无意义磁盘写入。
            if pct_changed:
                self._save_cache()
            try:
                stats_manager.record_status(st)
            except Exception as e:
                print(f"[Engine] 记录统计失败: {e}")

        if changed:
            print(f"[Engine] {st.device_name}: {st.percent}%"
                  f"{' 充电中' if st.charging else ''} ({st.connection_mode})", flush=True)
            self._fire_update()

    def _on_open_failed(self, dev_id: str, reason: str):
        """监听线程打开设备失败（多为官方驱动独占接口）：标记原因供 UI 明确提示"""
        changed = False
        with self._lock:
            curr = self.statuses.get(dev_id)
            if curr is not None and curr.raw_info != reason:
                curr.raw_info = reason
                changed = True
        if changed:
            self._fire_update()

    def _set_placeholder_online(self, cfg: Dict[str, Any]):
        """接收器在位：确保占位状态存在且标记在线"""
        dev_id = cfg["id"]
        changed = False
        with self._lock:
            curr = self.statuses.get(dev_id)
            if curr is None:
                # 占位状态一律 percent=-1：缓存值（_last_known_battery）仅供
                # 统计/趋势，不得包装成"在线电量"展示（虚假数据根因）
                self.statuses[dev_id] = BatteryStatus(
                    device_id=dev_id,
                    device_name=cfg.get("name", dev_id),
                    device_type=cfg.get("device_type", "mouse"),
                    percent=-1,
                    charging=False,
                    online=True,
                    connection_mode="2.4G",
                    timestamp=time.time(),
                    raw_info="WaitingForFirstPacket"
                )
                changed = True
            elif not curr.online:
                # 重新上线：仅恢复在线标记；若此前电量非真实读数则清零等真实帧
                curr.online = True
                curr.connection_mode = "2.4G"
                if "Cached" in (curr.raw_info or "") or "InitialSync" in (curr.raw_info or ""):
                    curr.percent = -1
                    curr.raw_info = "WaitingForFirstPacket"
                changed = True
        if changed:
            self._fire_update()

    def _mark_offline(self, dev_id: str):
        """接收器拔出：标记离线但保留最后已知电量"""
        changed = False
        with self._lock:
            curr = self.statuses.get(dev_id)
            if curr and curr.online:
                curr.online = False
                curr.connection_mode = "Unknown"
                changed = True
        if changed:
            self._fire_update()

    def _fire_update(self):
        if not self.on_update_callback:
            return
        try:
            with self._lock:
                copied = dict(self.statuses)
            self.on_update_callback(copied)
        except Exception:
            pass

    # ──────────────────────── 总线对账 ────────────────────────

    def poll_once(self):
        """扫描总线一次：处理接收器插拔、维持/重建每设备的监听线程"""
        try:
            bus_devices = hid.enumerate()
        except Exception:
            return

        with self._lock:
            configs = {k: v for k, v in self.devices_config.items()}

        # 1. 移除已删除或已禁用设备的监听
        for dev_id in list(self._listeners.keys()):
            if dev_id not in configs:
                self._listeners[dev_id].stop()
                del self._listeners[dev_id]

        # 2. 逐台设备对账
        for dev_id, cfg in configs.items():
            path, _score = self._select_interface(cfg, bus_devices)

            listener = self._listeners.get(dev_id)

            if path is None:
                # 接收器不在位
                if listener is not None:
                    listener.stop()
                    del self._listeners[dev_id]
                self._mark_offline(dev_id)
                continue

            # 接收器在位
            self._set_placeholder_online(cfg)

            # 路径变化（换 USB 口）或监听线程已退出（如设备被官方驱动
            # 独占打开失败）则重建监听
            if (listener is not None
                    and (listener.path != path or not listener.is_alive())):
                listener.stop()
                listener = None
                del self._listeners[dev_id]

            if listener is None:
                protocol = protocol_registry.get(cfg.get("protocol", ""))
                if protocol is None:
                    print(f"[Engine] 设备 {dev_id} 指定了未知协议: {cfg.get('protocol')}")
                    continue
                listener = DeviceListener(cfg, protocol, self._on_listener_status,
                                          self._on_open_failed)
                self._listeners[dev_id] = listener
                listener.start(path)

    # ──────────────────────── 生命周期 ────────────────────────

    def start(self):
        """启动后台总线对账线程"""
        if self._worker_thread and self._worker_thread.is_alive():
            return
        self._stop_event.clear()
        self._worker_thread = threading.Thread(target=self._run_loop,
                                               name="DeviceEngineWorker", daemon=True)
        self._worker_thread.start()

    def stop(self):
        """停止对账线程及全部设备监听"""
        self._stop_event.set()
        for listener in list(self._listeners.values()):
            try:
                listener.stop()
            except Exception:
                pass
        self._listeners.clear()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=1.0)
        self._worker_thread = None

    def reload_devices(self):
        """热重载设备配置（添加新外设后无需重启程序）

        停止全部监听 → 重扫 devices/ 目录 → 立即对账拉起新设备。
        注意：load_device_configs() 内部会抢 self._lock（不可重入），
        因此这里绝不能持有锁调用它。
        """
        print("[Engine] 热重载设备配置...")
        for listener in list(self._listeners.values()):
            try:
                listener.stop()
            except Exception:
                pass
        with self._lock:
            self._listeners.clear()
        # 锁外重扫配置
        self.load_device_configs()
        self.poll_once()
        self._fire_update()
        print(f"[Engine] 热重载完成，当前设备: {list(self.devices_config.keys())}")

    def _run_loop(self):
        self.poll_once()
        while not self._stop_event.is_set():
            # 3 秒一次全总线对账：hid.enumerate() 在 Windows 上要遍历全部 HID
            # 设备（几十毫秒），2 秒一次等于每天白跑 4 万次。3 秒对热插拔
            # 感知几乎无差别（人手插拔的感知阈值在 1 秒以上）。
            time.sleep(3.0)
            if self._stop_event.is_set():
                break
            self.poll_once()

    # ──────────────────────── 查询接口 ────────────────────────

    def get_all_statuses(self) -> Dict[str, BatteryStatus]:
        with self._lock:
            return dict(self.statuses)

    def get_status(self, device_id: str) -> Optional[BatteryStatus]:
        with self._lock:
            return self.statuses.get(device_id)
