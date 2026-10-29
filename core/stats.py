"""
电量历史统计：SQLite 落库、放电速率、续航估算。

性能设计（2026-10-29 重构）：
- 单例连接 + WAL：旧实现每次查询都 sqlite3.connect() 新建连接（UI 400ms 循环
  每秒新建 25 个连接），且 `with conn` 只提交不关闭。
- TTL 缓存：界面 400ms 刷新只命中内存缓存，SQL 实际每 3 秒最多跑一次。
- 线程安全：engine 监听线程写、UI 线程读，全部走同一把 RLock。
"""
import sqlite3
import time
import os
import csv
import threading
from typing import Optional, List, Dict, Tuple
from core.battery import BatteryStatus
from config import DB_FILE


class StatsManager:
    # 派生数据（速率/续航/历史）内存缓存秒数：小于等于 400ms UI 周期的几十倍，
    # 既保证"电量一变立刻准"，又不至于把 SQLite 打爆。
    CACHE_TTL = 3.0
    # 历史记录保留天数（超过自动归档清理，防止 DB 无限增长）
    PRUNE_DAYS = 90

    # 电量不变时的"保活"落库间隔（秒）。
    # 必须 <= _walk_discharge 的时间间隔上限，否则保活点会被当成"休眠间隙"
    # 全部剔除，导致电量长期平稳的设备永远算不出耗电速率（旧实现正是 900s
    # 保活 + 600s 上限，自相矛盾，速率基本永远出不来）。
    KEEPALIVE_SEC = 600
    # 相邻两条记录间隔超过这个值就视为设备休眠/关机，不计入活跃耗电时长
    MAX_ACTIVE_GAP = 900

    def __init__(self, db_path: str = DB_FILE):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()
        # device_id -> (percent, charging, timestamp)
        self._last_recorded: Dict[str, Tuple[int, bool, float]] = {}
        self._restore_last_recorded()
        # (device_id, hours) -> (expire_at, records)
        self._history_cache: Dict[Tuple[str, int], Tuple[float, List[Tuple[float, int, bool]]]] = {}
        # device_id -> (expire_at, discharge_state)
        self._derived_cache: Dict[str, Tuple[float, Dict]] = {}
        self._last_prune = 0.0

    # ──────────────────────── 连接与建表 ────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        """单例连接。check_same_thread=False + 外部 RLock 保证跨线程安全。"""
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=5.0)
            self._conn.row_factory = sqlite3.Row
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
            except Exception:
                pass
        return self._conn

    def _init_db(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with self._lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS battery_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                percent INTEGER NOT NULL,
                charging INTEGER NOT NULL,
                online INTEGER NOT NULL,
                connection_mode TEXT
            )
            """)
            cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_dev_time ON battery_records (device_id, timestamp)
            """)
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS charge_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                start_time REAL NOT NULL,
                end_time REAL,
                start_pct INTEGER,
                end_pct INTEGER
            )
            """)
            cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_event_dev ON charge_events (device_id, id)
            """)
            conn.commit()

    def _restore_last_recorded(self):
        """从库里恢复每台设备最后一条记录。

        旧实现只存内存，程序重启后 _last_recorded 为空 → 重启瞬间不会识别
        "未充电→充电"的跃迁，充电事件被漏记。
        """
        try:
            with self._lock:
                conn = self._get_conn()
                rows = conn.execute("""
                    SELECT r.device_id, r.percent, r.charging, r.timestamp
                    FROM battery_records r
                    JOIN (SELECT device_id, MAX(id) AS mid FROM battery_records GROUP BY device_id) m
                      ON r.id = m.mid
                """).fetchall()
                for r in rows:
                    self._last_recorded[r["device_id"]] = (
                        int(r["percent"]), bool(r["charging"]), float(r["timestamp"])
                    )
        except Exception as e:
            print(f"[Stats] 恢复上次记录状态失败: {e}")

    def _invalidate(self, device_id: Optional[str] = None):
        """写库后让相关缓存立即失效（电量变了，界面必须立刻看到新值）"""
        if device_id is None:
            self._history_cache.clear()
            self._derived_cache.clear()
            return
        for key in [k for k in self._history_cache if k[0] == device_id]:
            self._history_cache.pop(key, None)
        self._derived_cache.pop(device_id, None)

    # ──────────────────────── 写入 ────────────────────────

    def record_status(self, status: BatteryStatus):
        if not status.is_valid:
            return

        now = status.timestamp
        last = self._last_recorded.get(status.device_id)

        # 电量变化 / 充电状态变化 / 15 分钟保活点 三者之一才写库
        should_record = False
        if last is None:
            should_record = True
        else:
            last_pct, last_chg, last_time = last
            if last_pct != status.percent or last_chg != status.charging:
                should_record = True
            elif (now - last_time) >= self.KEEPALIVE_SEC:
                should_record = True

        if not should_record:
            return

        with self._lock:
            conn = self._get_conn()
            cursor = conn.cursor()
            try:
                cursor.execute("""
                INSERT INTO battery_records (device_id, timestamp, percent, charging, online, connection_mode)
                VALUES (?, ?, ?, ?, ?, ?)
                """, (status.device_id, now, status.percent,
                      1 if status.charging else 0, 1 if status.online else 0,
                      status.connection_mode))

                if last is not None:
                    _, last_chg, _ = last
                    if not last_chg and status.charging:
                        cursor.execute("""
                        INSERT INTO charge_events (device_id, start_time, start_pct)
                        VALUES (?, ?, ?)
                        """, (status.device_id, now, status.percent))
                    elif last_chg and not status.charging:
                        # 旧实现直接写 UPDATE ... ORDER BY id DESC LIMIT 1，
                        # 标准 CPython 的 SQLite 未编译 UPDATE_DELETE_LIMIT，
                        # 会抛 OperationalError 并把同一事务里的 INSERT 一起回滚。
                        # 改为先取 id，再按主键更新。
                        row = cursor.execute(
                            "SELECT id FROM charge_events WHERE device_id = ? AND end_time IS NULL "
                            "ORDER BY id DESC LIMIT 1",
                            (status.device_id,)).fetchone()
                        if row is not None:
                            cursor.execute(
                                "UPDATE charge_events SET end_time = ?, end_pct = ? WHERE id = ?",
                                (now, status.percent, row["id"]))
                conn.commit()
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                print(f"[Stats] 写入电量记录失败: {e}")
                return

            self._last_recorded[status.device_id] = (status.percent, status.charging, now)
            self._invalidate(status.device_id)

        self._maybe_prune()

    def _maybe_prune(self):
        """每天最多清理一次：删除超过 PRUNE_DAYS 天的明细，避免 DB 无限膨胀"""
        now = time.time()
        if now - self._last_prune < 86400:
            return
        self._last_prune = now
        cutoff = now - self.PRUNE_DAYS * 86400
        try:
            with self._lock:
                conn = self._get_conn()
                cur = conn.execute("DELETE FROM battery_records WHERE timestamp < ?", (cutoff,))
                deleted = cur.rowcount or 0
                conn.execute("DELETE FROM charge_events WHERE end_time IS NOT NULL AND end_time < ?",
                             (cutoff,))
                conn.commit()
                if deleted:
                    print(f"[Stats] 已归档清理 {deleted} 条超过 {self.PRUNE_DAYS} 天的历史记录")
        except Exception as e:
            print(f"[Stats] 历史清理失败: {e}")

    # ──────────────────────── 读取 ────────────────────────

    def get_history(self, device_id: str, hours: int = 72) -> List[Tuple[float, int, bool]]:
        """获取近 hours 小时的电量历史 [(timestamp, percent, charging), ...]（带 3 秒缓存）"""
        key = (device_id, hours)
        now = time.time()
        cached = self._history_cache.get(key)
        if cached is not None and cached[0] > now:
            return cached[1]

        cutoff = now - (hours * 3600)
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute("""
            SELECT timestamp, percent, charging FROM battery_records
            WHERE device_id = ? AND timestamp >= ?
            ORDER BY timestamp ASC
            """, (device_id, cutoff)).fetchall()
        data = [(r["timestamp"], r["percent"], bool(r["charging"])) for r in rows]
        self._history_cache[key] = (now + self.CACHE_TTL, data)
        return data

    @staticmethod
    def _walk_discharge(records: List[Tuple[float, int, bool]]) -> Tuple[float, int]:
        """遍历记录累计「活跃放电秒数」与「掉的百分点」。

        只统计非充电状态、且相邻两点间隔不超过 MAX_ACTIVE_GAP 的连续区间
        （间隔过大说明设备休眠/关机，不能计入耗电时长）。
        """
        active_seconds = 0.0
        drop = 0
        max_gap = StatsManager.MAX_ACTIVE_GAP
        for i in range(1, len(records)):
            t_prev, p_prev, c_prev = records[i - 1]
            t_curr, p_curr, c_curr = records[i]
            if not c_prev and not c_curr:
                dt = t_curr - t_prev
                if 0 < dt <= max_gap:
                    dp = p_prev - p_curr
                    if dp >= 0:
                        active_seconds += dt
                        drop += dp
        return active_seconds, drop

    def get_discharge_state(self, device_id: str, hours_window: int = 48) -> Dict:
        """
        返回放电估算的可读状态，供界面区分「还在采集 / 电量稳定 / 已有速率」。
        :return: {"state": "no_data"|"collecting"|"stable"|"ok", "rate": float|None,
                  "active_minutes": float, "drop": int, "records": int}
        """
        now = time.time()
        cached = self._derived_cache.get(device_id)
        if cached is not None and cached[0] > now:
            return cached[1]

        records = self.get_history(device_id, hours=hours_window)
        active_seconds, drop = self._walk_discharge(records)

        if len(records) < 2:
            state = "no_data"
        elif drop > 0 and active_seconds >= 600:
            state = "ok"
        elif active_seconds >= 600:
            state = "stable"       # 活跃时长达标，但电量还没掉
        else:
            state = "collecting"   # 采集时长不足

        rate = round(drop / (active_seconds / 3600.0), 2) if state == "ok" else None
        result = {
            "state": state,
            "rate": rate,
            "active_minutes": round(active_seconds / 60.0, 1),
            "drop": drop,
            "records": len(records),
        }
        self._derived_cache[device_id] = (now + self.CACHE_TTL, result)
        return result

    def calculate_active_discharge_rate(self, device_id: str, hours_window: int = 48) -> Optional[float]:
        """活跃放电速率（%/小时）；样本不足或电量未下降时返回 None（绝不给假数值）"""
        return self.get_discharge_state(device_id, hours_window).get("rate")

    # 常见外设标称典型全电量工作时长基准 (小时)
    NOMINAL_BATTERY_HOURS = {
        "lingbao_k98": 90.0,   # 4000mAh 机械键盘带背光/低功耗无线约 90 小时
        "mchose_g3v2": 65.0,   # 500mAh 电竞鼠标 1K 回报率约 65 小时
    }

    def estimate_remaining_hours(self, device_id: str, current_percent: int) -> float:
        """
        智能结合算法：基于硬件标称给出基准，使用中动态根据实际耗电速率校准。
        实测速率与标称按 7:3 融合，避免单次激进操作导致估算暴涨暴跌。
        """
        if current_percent <= 0:
            return 0.0

        nominal_full_hours = self.NOMINAL_BATTERY_HOURS.get(device_id, 75.0)
        base_rate = 100.0 / nominal_full_hours          # 基准耗电速率 (%/h)

        # 复用缓存过的派生状态，避免重复扫 48 小时历史（旧实现每次都要重算一遍）
        measured_rate = self.get_discharge_state(device_id).get("rate")
        if measured_rate and measured_rate > 0.1:
            effective_rate = round(0.7 * measured_rate + 0.3 * base_rate, 2)
        else:
            effective_rate = round(base_rate, 2)

        return max(0.5, round(current_percent / effective_rate, 1))

    def format_remaining_time(self, hours: float) -> str:
        """将剩余可用小时格式化为直观展示文本，如 '约 52.0 小时 (约 3.2 天)'"""
        if hours <= 0:
            return "即将耗尽"
        days = round(hours / 16.0, 1)  # 按每天正常使用 16 小时折算工作天数
        if days >= 1.0:
            return f"约 {hours} 小时 (约 {days} 天)"
        return f"约 {hours} 小时"

    def get_time_since_last_full_charge(self, device_id: str) -> Optional[float]:
        """距离上次充满电（电量达到 100% 或充电结束）的经过秒数"""
        with self._lock:
            conn = self._get_conn()
            row = conn.execute("""
            SELECT timestamp FROM battery_records
            WHERE device_id = ? AND percent = 100
            ORDER BY timestamp DESC LIMIT 1
            """, (device_id,)).fetchone()
            if row:
                return time.time() - row["timestamp"]

            # 兜底：最近一次充电结束事件
            row = conn.execute("""
            SELECT end_time FROM charge_events
            WHERE device_id = ? AND end_time IS NOT NULL
            ORDER BY id DESC LIMIT 1
            """, (device_id,)).fetchone()
            if row and row["end_time"]:
                return time.time() - row["end_time"]

        return None

    def get_recent_charge_cycles(self, device_id: str, limit: int = 10) -> List[Dict]:
        """获取近期充放电事件"""
        with self._lock:
            conn = self._get_conn()
            rows = conn.execute("""
            SELECT start_time, end_time, start_pct, end_pct FROM charge_events
            WHERE device_id = ?
            ORDER BY id DESC LIMIT ?
            """, (device_id, limit)).fetchall()
        res = []
        for r in rows:
            duration_min = round((r["end_time"] - r["start_time"]) / 60, 1) if r["end_time"] else None
            res.append({
                "start_time": r["start_time"],
                "end_time": r["end_time"],
                "start_pct": r["start_pct"],
                "end_pct": r["end_pct"],
                "duration_min": duration_min,
            })
        return res

    def get_charge_cycles(self, device_id: str) -> int:
        """获取该设备的充放电次数（从 charge_events 表统计）"""
        try:
            with self._lock:
                conn = self._get_conn()
                row = conn.execute(
                    "SELECT COUNT(*) FROM charge_events WHERE device_id = ?",
                    (device_id,)).fetchone()
                return row[0] if row else 0
        except Exception:
            return 0

    # ──────────────────────── 界面聚合 ────────────────────────

    def get_ui_snapshot(self, device_id: str, percent: int) -> Dict:
        """
        一次取齐界面需要的全部派生数据（速率 / 剩余时长 / 上次充满 / 循环次数 / 历史）。
        旧实现 push_all_statuses 每 400ms 每设备要跑 5 次查询并重复算两次耗电速率，
        现在合并成 1 次并全部走 3 秒缓存。
        """
        state = self.get_discharge_state(device_id)
        rate = state.get("rate")

        hours = self.estimate_remaining_hours(device_id, percent)
        rem_text = "外设已休眠待命" if percent <= 0 else self.format_remaining_time(hours)

        sec_since_full = self.get_time_since_last_full_charge(device_id)
        if sec_since_full is None:
            last_full = "初次记录中"
        elif sec_since_full < 3600:
            last_full = f"{max(1, int(sec_since_full // 60))} 分钟前"
        elif sec_since_full < 86400:
            last_full = f"{round(sec_since_full / 3600, 1)} 小时前"
        else:
            last_full = f"{round(sec_since_full / 86400, 1)} 天前"

        cycles = self.get_charge_cycles(device_id)
        cycle_text = f"循环 {cycles} 次" if cycles > 0 else "健康监控中"

        history_raw = self.get_history(device_id, hours=168)
        history_pts = [{"t": round(r[0], 1), "p": r[1], "c": bool(r[2])} for r in history_raw]

        return {
            "rate": rate,
            "remText": rem_text,
            "lastFull": last_full,
            "cycleText": cycle_text,
            "history": history_pts,
            "dischargeState": state.get("state"),
        }

    def export_csv(self, device_id: str, target_file: str) -> bool:
        """导出指定设备的历史电量记录为 CSV"""
        try:
            with self._lock:
                conn = self._get_conn()
                rows = conn.execute("""
                SELECT id, device_id, timestamp, percent, charging, online, connection_mode
                FROM battery_records WHERE device_id = ? ORDER BY timestamp ASC
                """, (device_id,)).fetchall()

            with open(target_file, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["ID", "设备标识", "记录时间", "电量(%)", "是否充电", "在线状态", "连接模式"])
                for row in rows:
                    timestr = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["timestamp"]))
                    writer.writerow([
                        row["id"], row["device_id"], timestr,
                        row["percent"], "是" if row["charging"] else "否",
                        "在线" if row["online"] else "离线", row["connection_mode"],
                    ])
            return True
        except Exception as e:
            print(f"[Stats] Export CSV failed: {e}")
            return False

    def export_all_csv(self, target_file: str) -> int:
        """把全部设备历史导出到同一个 CSV（托盘「导出全部」用，避免后者覆盖前者）"""
        try:
            with self._lock:
                conn = self._get_conn()
                rows = conn.execute("""
                SELECT id, device_id, timestamp, percent, charging, online, connection_mode
                FROM battery_records ORDER BY device_id ASC, timestamp ASC
                """).fetchall()

            with open(target_file, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["ID", "设备标识", "记录时间", "电量(%)", "是否充电", "在线状态", "连接模式"])
                for row in rows:
                    timestr = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["timestamp"]))
                    writer.writerow([
                        row["id"], row["device_id"], timestr,
                        row["percent"], "是" if row["charging"] else "否",
                        "在线" if row["online"] else "离线", row["connection_mode"],
                    ])
            return len(rows)
        except Exception as e:
            print(f"[Stats] Export all CSV failed: {e}")
            return 0


stats_manager = StatsManager()
