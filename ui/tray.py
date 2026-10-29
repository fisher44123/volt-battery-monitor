"""
托盘管理器：任务栏电池图标 + 左键呼出控制中心 + 右键原生菜单
- 左键单击 / 双击：秒级唤出/聚焦控制中心主面板
- 悬浮光标：展示所有外设的实时电量与连接状态
- 右键菜单：原生 wx.Menu（设备状态区 + 操作区 + 退出）
"""
import os
import time
import subprocess
import wx
import wx.adv
from typing import Dict, Optional, Callable
from core.battery import BatteryStatus
from core.engine import DeviceEngine
from core.stats import stats_manager
from ui.icons import create_wx_icon, create_dual_wx_icon
from ui.osd import osd_manager
from config import config

# ─────────────────────────────────────────────────────────────────────
# MCHOSE HUB 进程检测
# 注意：AGENTS.md 硬约束第 1 条已实机验证「与 MCHOSE HUB 完美共存免杀直读」，
# 官方驱动不再独占供应商集合，所以这个检测只用于给出友好提示，不再是读取前提。
# 每次弹卡片都起一个 tasklist.exe 子进程（约 50ms）太浪费，结果缓存 30 秒。
# ─────────────────────────────────────────────────────────────────────
_HUB_CACHE = {"ts": 0.0, "running": False}
_HUB_CACHE_TTL = 30.0


def is_mchose_hub_running() -> bool:
    """检测 MCHOSE HUB 是否在运行（结果缓存 30 秒，避免反复起子进程）"""
    now = time.time()
    if now - _HUB_CACHE["ts"] < _HUB_CACHE_TTL:
        return _HUB_CACHE["running"]

    running = False
    try:
        r = subprocess.run(
            ["tasklist", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for line in r.stdout.splitlines():
            if "MCHOSE" in line.upper():
                running = True
                break
    except Exception:
        running = False

    _HUB_CACHE["ts"] = now
    _HUB_CACHE["running"] = running
    return running


# 电量颜色 (现代高对比度质感色系)
BATTERY_COLORS = {
    "charging": wx.Colour(0, 122, 255),
    "normal":   wx.Colour(16, 185, 129),
    "warn":     wx.Colour(245, 158, 11),
    "crit":     wx.Colour(239, 68, 68),
    "offline":  wx.Colour(140, 148, 160),
}


def get_battery_color(status: BatteryStatus) -> wx.Colour:
    if not status.online or status.percent < 0:
        return BATTERY_COLORS["offline"]
    if status.charging:
        return BATTERY_COLORS["charging"]
    # 阈值接活配置项（旧实现硬编码 20 / 10，settings 里那两项形同虚设）
    warn_at = int(config.get("warning_threshold", 20) or 20)
    crit_at = int(config.get("critical_threshold", 10) or 10)
    if status.percent > warn_at:
        return BATTERY_COLORS["normal"]
    if status.percent > crit_at:
        return BATTERY_COLORS["warn"]
    return BATTERY_COLORS["crit"]


# ---------------------------------------------------------------------------
# 浅色卡片式托盘菜单（仿 MCHOSE M HUB 风格：白底 + 灰状态条 + 双按钮）
# 用无边框 wx.Frame：PopupTransientWindow 在右键 TrackPopupMenu 上下文中
# 弹出即失焦销毁（"没弹出"根因）；wx.Frame 主动 Show+Raise+SetFocus 持焦点。
# ---------------------------------------------------------------------------
# 托盘卡片已迁移到 ui/tray_card.py（GraphicsContext 抗锯齿重绘版）。
# 旧实现是 250 行 PaintDC 代码，只画出几行文字 + 两个纯文字按钮，
# 与系统右键菜单无异，已整体替换。
from ui.tray_card import TrayCardPopup  # noqa: E402  (保持下方引用不变)

# ---------------------------------------------------------------------------
# 托盘图标 + 右键原生菜单 (带状态预览、打开控制中心、退出)
# ---------------------------------------------------------------------------
class RapooStyleTrayIcon(wx.adv.TaskBarIcon):
    def __init__(self, engine: DeviceEngine):
        super().__init__()
        self.engine = engine
        self._popup = None
        self._prev_status_records = {}
        self._icon_sig = None      # 上次图标签名，内容没变就不必重绘重设

        # 左键单击 / 双击：直接唤出/聚焦控制中心面板
        self.Bind(wx.adv.EVT_TASKBAR_LEFT_DOWN, self.on_left_click)
        self.Bind(wx.adv.EVT_TASKBAR_LEFT_DCLICK, self.on_left_click)
        # 右键：弹浅色状态卡片（AGENTS.md 硬约束第 5 条）。
        # 这里必须显式 Bind —— 旧代码只写了 on_right_click 却从未绑定，
        # 导致精心绘制的 TrayCardPopup 变成永远走不到的死代码，
        # 实际弹出来的是 wx 默认的 CreatePopupMenu 原生菜单。
        # 绑定后 ProcessEvent 返回 True，wx 就不会再弹默认菜单。
        self.Bind(wx.adv.EVT_TASKBAR_RIGHT_UP, self.on_right_click)

    def CreatePopupMenu(self):
        """右键原生上下文菜单（满足用户在右下角直接退出的核心习惯，100% 可见可靠）"""
        menu = wx.Menu()

        # 1. 设备实时状态条目（只读展示）
        statuses = self.engine.get_all_statuses()
        for st in statuses.values():
            icon_tag = "⌨" if st.device_type != "mouse" else "🖱"
            pct_str = f"{st.percent}%" if st.percent > 0 else "待同步"
            if st.charging:
                pct_str += " (⚡充电中)"
            item = menu.Append(wx.ID_ANY, f"{icon_tag} {st.device_name}: {pct_str}")
            item.Enable(False)

        if not statuses:
            item = menu.Append(wx.ID_ANY, "未检测到 2.4G 外设")
            item.Enable(False)

        menu.AppendSeparator()

        # 2. 控制操作
        m_dash = menu.Append(wx.ID_ANY, "打开 Volt 控制中心")
        self.Bind(wx.EVT_MENU, lambda e: self._toggle_dashboard(), m_dash)

        m_refresh = menu.Append(wx.ID_ANY, "立即检测硬件")
        self.Bind(wx.EVT_MENU, lambda e: self._on_refresh(), m_refresh)

        menu.AppendSeparator()

        # 3. 退出功能（用户在右键右下角必须能看到的退出功能）
        m_exit = menu.Append(wx.ID_ANY, "退出 Volt")
        self.Bind(wx.EVT_MENU, lambda e: self._on_exit(None), m_exit)

        return menu

    def on_right_click(self, event):
        # 唯一弹窗触发点：EVT_TASKBAR_RIGHT_UP 事件
        try:
            self._show_card_popup()
        except Exception as e:
            import traceback
            print(f"[Tray] 卡片弹窗失败: {e}")
            traceback.print_exc()

    def _show_card_popup(self):
        """每次右键都重建卡片。

        坑（实测踩过）：卡片 dismiss 后 C++ 对象已销毁，但 Python 包装器
        还被 self._popup 引用着。此时调用 self._popup.IsShown() 会抛
        RuntimeError: wrapped C/C++ object ... has been deleted，
        表现就是「第一次右键能弹，之后每次右键都静默失灵」。
        所以先把引用摘下来，再用 bool()（wxPython 对已销毁窗口返回 False）
        + try/except 双重判断。
        """
        old = self._popup
        self._popup = None

        if old is not None:
            alive = False
            try:
                alive = bool(old) and old.IsShown()
            except RuntimeError:
                alive = False
            except Exception:
                alive = False

            if alive:
                self._popup = old       # 已经在显示，保持原样
                return

            try:
                if bool(old):
                    old.Destroy()
            except Exception:
                pass

        try:
            popup = TrayCardPopup(self.engine, {
                "dashboard": self._toggle_dashboard,
                "exit": self._on_exit,
            })
            self._popup = popup
            popup.show_at_cursor()
        except Exception as e:
            import traceback
            print(f"[Tray] 弹窗创建异常: {e}")
            traceback.print_exc()
            traceback.print_exc()

    def on_left_click(self, event):
        """左键单击：打开/聚焦控制中心面板"""
        self._toggle_dashboard()

    def _toggle_dashboard(self):
        from ui.webview_dashboard import WebViewDashboardFrame
        app = wx.GetApp()
        frame = getattr(app, "_dashboard", None)
        if frame is None or not frame:
            frame = WebViewDashboardFrame(None, self.engine)
            app._dashboard = frame
            frame.Show()
        else:
            if frame.IsShown():
                if frame.IsIconized():
                    frame.Restore()
                frame.Raise()
            else:
                frame.on_opened()
                frame.Show()
                frame.Raise()

    def update_tray(self, all_statuses: Dict[str, BatteryStatus]):
        """更新托盘图标与悬浮提示（双设备动态电量柱 + 智能 OSD 预警）"""
        warn_at = int(config.get("warning_threshold", 20) or 20)
        crit_at = int(config.get("critical_threshold", 10) or 10)

        # 1. 状态跃迁检测：插上充电器 / 拔出充满 / 低电量跌破警戒线时弹 OSD 胶囊
        for dev_id, s in all_statuses.items():
            if not s or not s.online or s.percent <= 0:
                continue
            prev = self._prev_status_records.get(dev_id)
            if prev is not None:
                prev_pct, prev_chg = prev
                # 刚刚接通电源开始充电
                if not prev_chg and s.charging:
                    self._safe_osd(all_statuses)
                # 充至满电
                elif s.charging and prev_pct < 100 and s.percent >= 100:
                    self._safe_osd(all_statuses)
                # 未充电状态下跌破预警/告警线（阈值来自 settings.json）
                elif not s.charging and (
                        (prev_pct > warn_at >= s.percent) or
                        (prev_pct > crit_at >= s.percent)):
                    self._safe_osd(all_statuses)
            self._prev_status_records[dev_id] = (s.percent, s.charging)

        # 2. 图标按内容签名缓存：电量/充电/在线/休眠都没变就不重绘重设
        #    （旧实现每次状态回调都重新跑一遍 PIL 并 SetIcon，任务栏图标会闪）
        sig = tuple(sorted(
            (s.device_id,
             s.percent if s.online else -2,
             s.charging,
             getattr(s, "raw_info", "") == "Sleeping")
            for s in all_statuses.values()
        ))
        if sig == self._icon_sig:
            return
        self._icon_sig = sig

        icon = create_dual_wx_icon(all_statuses, size=32)

        # 3. 构造任务栏 Tooltip
        lines = ["Volt 外设控制中心"]
        for s in all_statuses.values():
            icon_tag = "鼠标" if s.device_type == "mouse" else "键盘"
            pct_str = f"{s.percent}%" if s.percent > 0 else "待同步"
            chg_str = " (⚡充电中)" if s.charging else ""
            sleep_str = " (💤休眠中)" if getattr(s, "raw_info", "") == "Sleeping" else ""
            lines.append(f"[{icon_tag}] {s.device_name}: {pct_str}{chg_str}{sleep_str}")
        tip = "\n".join(lines)

        if len(tip) > 120:
            tip = tip[:117] + "..."

        try:
            ret = self.SetIcon(icon, tip)
        except Exception:
            ret = False

        if not ret:
            retries = getattr(self, "_retry_count", 0)
            if retries < 3 and not getattr(self, "_retry_scheduled", False):
                self._retry_scheduled = True
                self._retry_count = retries + 1
                self._icon_sig = None      # 下次强制重试
                wx.CallLater(3000, self._retry_update)
        else:
            self._retry_count = 0

    @staticmethod
    def _safe_osd(all_statuses):
        try:
            osd_manager.show(all_statuses)
        except Exception as e:
            print(f"[Tray] OSD 弹出失败: {e}")

    def _retry_update(self):
        self._retry_scheduled = False
        self.update_tray(self.engine.get_all_statuses())

    def _on_refresh(self):
        self.engine.poll_once()
        self.update_tray(self.engine.get_all_statuses())
        app = wx.GetApp()
        frame = getattr(app, "_dashboard", None)
        if frame and frame.IsShown():
            frame.push_all_statuses()

    def _on_add_device(self):
        """打开一键添加外设向导（保存后热重载引擎）"""
        from ui.device_wizard import DeviceWizardDialog
        parent = wx.GetApp().GetTopWindow()
        dlg = DeviceWizardDialog(parent, on_saved_callback=self.engine.reload_devices)
        dlg.ShowModal()
        dlg.Destroy()
        self._on_refresh()

    def _on_export_data(self, evt):
        """导出全部设备历史到同一个 CSV。

        旧实现对每台设备调用 export_csv(同一個路径)，后写的直接覆盖先写的，
        导出来只剩最后一台设备的数据。改为一次性导出全部。
        """
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        target_csv = os.path.join(desktop, "外设电量历史数据.csv")
        rows = stats_manager.export_all_csv(target_csv)
        app = wx.GetApp()
        frame = getattr(app, "_dashboard", None)
        if rows > 0:
            msg = f"已导出 {rows} 条记录到桌面"
            kind = "ok"
        else:
            msg = "暂未产生记录，使用一段时间后再导出"
            kind = "warn"
        if frame is not None and getattr(frame, "_web_ready", False):
            frame.show_toast(msg, kind)
        else:
            print(f"[Tray] 导出结果: {msg}")

    def _on_exit(self, evt):
        self.RemoveIcon()
        self.Destroy()
        osd_manager.destroy()
        app = wx.GetApp()
        frame = getattr(app, "_dashboard", None)
        if frame:
            try:
                frame.Destroy()
            except Exception:
                pass
        anchor = getattr(app, "_anchor", None)
        if anchor:
            try:
                anchor.Destroy()
            except Exception:
                pass
        app.ExitMainLoop()


class TrayManager:
    def __init__(self, engine: DeviceEngine):
        self.engine = engine
        self.tray_icon = RapooStyleTrayIcon(engine)
        # 右键由 wxPython 自动触发 CreatePopupMenu（已重写为卡片弹出）
        self._ready = False
        wx.CallLater(300, self._first_update)

    def _first_update(self):
        self._ready = True
        self.update_all()

    def update_all(self):
        if not self._ready:
            return
        statuses = self.engine.get_all_statuses()
        self.tray_icon.update_tray(statuses)

    def destroy_all(self):
        self.tray_icon.RemoveIcon()
        self.tray_icon.Destroy()
