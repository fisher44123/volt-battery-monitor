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
class TrayCardPopup(wx.Frame):
    """右键托盘图标弹出的浅色卡片菜单：上部设备状态条 + 下部双按钮"""

    ROW_H = 44          # 状态行高度
    BTN_H = 48          # 按钮区高度
    WIDTH = 400         # 加宽到 400，确保高 DPI 下文字与按钮宽裕
    PAD = 12
    RADIUS = 10

    # Volt 2.0 现代轻量美学主题 (冷浅灰 + 纯白 + 科技蓝)
    CARD_BG   = wx.Colour(0xFF, 0xFF, 0xFF)
    STATUS_BG = wx.Colour(0xF8, 0xFA, 0xFC)   # 状态条高质冷浅灰
    BORDER    = wx.Colour(0xE2, 0xE8, 0xF0)   # 细腻分割线
    TEXT_MAIN = wx.Colour(0x0F, 0x17, 0x2A)   # 深近黑主标题
    TEXT_SUB  = wx.Colour(0x47, 0x55, 0x69)   # 次级文字
    TEXT_FAINT= wx.Colour(0x94, 0xA3, 0xB8)
    HOVER     = wx.Colour(0xF1, 0xF5, 0xF9)   # 悬浮微光
    ACCENT    = wx.Colour(0x00, 0x66, 0xFF)   # 活力科技蓝
    DANGER    = wx.Colour(0xEF, 0x44, 0x44)   # 退出红
    DIVIDER   = wx.Colour(0xF1, 0xF5, 0xF9)   # 分隔线

    def __init__(self, engine: DeviceEngine, callbacks: Dict[str, Callable]):
        # 无边框、不进任务栏、置顶的 Frame；parent 用应用锚窗（避免 Win32 断言）
        app = wx.GetApp()
        parent = None
        if app is not None:
            top = app.GetTopWindow()
            parent = top if top is not None else getattr(app, "_anchor", None)
        if parent is None:
            parent = wx.Frame(None, size=(1, 1), style=wx.NO_BORDER)
        super().__init__(parent, title="Volt Tray Menu",
                         style=wx.FRAME_NO_TASKBAR | wx.NO_BORDER | wx.STAY_ON_TOP)
        self.engine = engine
        self._cb = callbacks          # {"dashboard":..., "exit":...}
        self._hover_btn = -1          # -1 / 0=dashboard / 1=exit
        self._devices = []            # [(name, pct_str, dot_color, is_kb)]
        self._closing = False

        self.SetBackgroundColour(self.CARD_BG)
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_LEFT_DOWN, self._on_click)
        self.Bind(wx.EVT_MOTION, self._on_motion)
        self.Bind(wx.EVT_LEAVE_WINDOW, lambda e: self._set_hover(-1))
        self.Bind(wx.EVT_KEY_DOWN, self._on_key)
        self.Bind(wx.EVT_ACTIVATE, self._on_activate)

        self._build_devices()
        try:
            self.SetSize(self.FromDIP(wx.Size(self.WIDTH, self._calc_height())))
        except Exception:
            self.SetSize((self.WIDTH, self._calc_height()))

    # ── 显示 / 关闭 ──
    def show_at_cursor(self):
        """替代旧 Popup()：定位 + 显示 + 主动持焦点（防弹即毁）"""
        self.PositionAtCursor()
        self.Show()
        self.Raise()
        try:
            self.SetFocus()
        except Exception:
            pass

    def dismiss(self):
        """关闭并延迟销毁（避免事件重入中 Destroy 崩溃）"""
        if self._closing:
            return
        self._closing = True
        try:
            self.Hide()
        except Exception:
            pass
        wx.CallLater(80, self._destroy)

    def _destroy(self):
        try:
            self.Destroy()
        except Exception:
            pass

    # ── 数据 ──
    def _build_devices(self):
        self._devices = []
        statuses = self.engine.get_all_statuses()
        for st in statuses.values():
            is_kb = st.device_type != "mouse"
            if st.online and st.percent > 0:
                pct_str = f"{st.percent}%"
                dot = get_battery_color(st)
            else:
                pct_str = "待同步"
                dot = wx.Colour(0xB6, 0xBC, 0xC4)  # 离线灰
            self._devices.append((st.device_name, pct_str, dot, is_kb))
        if not statuses:
            self._devices.append(("未检测到外设", "", wx.Colour(0xB6, 0xBC, 0xC4), False))
        # 提示逻辑：迈从休眠时给一句可执行的动作（晃动鼠标）。
        # 旧实现在这里还会显示「请退出 MCHOSE HUB」—— 该结论已被 AGENTS.md
        # 第 1 条实机验证推翻（共存免杀、无需退出官方驱动），继续提示会误导用户。
        g3 = statuses.get("mchose_g3v2")
        if g3 is not None and not (g3.online and g3.percent > 0) \
                and getattr(g3, "raw_info", "") != "Sleeping":
            self._devices.append(("💡 晃动鼠标唤醒以刷新电量", "",
                                  wx.Colour(0x00, 0x53, 0xE2), False))

    def _calc_height(self) -> int:
        # 上部状态条（内衬 10px + 每设备 42px）+ 分隔 + 按钮区
        inner_h = len(self._devices) * self.ROW_H
        return self.PAD + inner_h + 18 + self.BTN_H + self.PAD

    # ── 绘制 ──
    def _on_paint(self, evt):
        dc = wx.PaintDC(self)
        dc.SetBackground(wx.Brush(self.CARD_BG))
        dc.Clear()
        w, h = self.GetSize()

        # 卡片边框
        dc.SetPen(wx.Pen(self.BORDER, 1))
        dc.SetBrush(wx.TRANSPARENT_BRUSH)
        dc.DrawRoundedRectangle(0, 0, w - 1, h - 1, self.RADIUS)

        font_name = wx.Font(10, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_NORMAL)
        font_bold = wx.Font(10, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)

        # ── 上部状态区（灰底圆角块）──
        inner_h = len(self._devices) * self.ROW_H
        status_rect = (self.PAD, self.PAD, w - 2 * self.PAD, inner_h + 8)
        dc.SetPen(wx.TRANSPARENT_PEN)
        dc.SetBrush(wx.Brush(self.STATUS_BG))
        dc.DrawRoundedRectangle(status_rect[0], status_rect[1],
                                status_rect[2], status_rect[3], 8)

        y = self.PAD + 4
        for name, pct_str, dot, is_kb in self._devices:
            # 提示行（💡/⚠ 前缀）红色显示、不画类型图标
            is_warn = name[:1] in ("💡", "⚠", "❗")
            # 类型图标（键盘/鼠标小 SVG 形状，简单绘制；警告行不画图标）
            icon_x = self.PAD + 12
            icon_cx = icon_x + 6
            icon_cy = y + self.ROW_H // 2
            if not is_warn:
                dc.SetBrush(wx.Brush(self.ACCENT))
                dc.SetPen(wx.TRANSPARENT_PEN)
                if is_kb:
                    dc.DrawRoundedRectangle(icon_cx - 8, icon_cy - 6, 16, 11, 2)
                    dc.DrawRoundedRectangle(icon_cx - 8, icon_cy - 6, 3, 11, 1)
                else:
                    dc.DrawEllipse(icon_cx - 7, icon_cy - 5, 14, 10)
                    dc.DrawRoundedRectangle(icon_cx - 4, icon_cy - 8, 8, 16, 3)
                    dc.DrawLine(icon_cx, icon_cy + 8, icon_cx - 4, icon_cy + 13)
                    dc.DrawLine(icon_cx, icon_cy + 8, icon_cx + 4, icon_cy + 13)

            # 设备名：超宽自动缩字号防截断（预留电量区 70px + 状态点 20px）
            name_x = icon_x + (0 if is_warn else 22)
            max_name_w = status_rect[0] + status_rect[2] - name_x - (72 if pct_str else 12)
            dc.SetFont(font_bold)
            name_font = font_bold
            if dc.GetTextExtent(name).GetWidth() > max_name_w:
                name_font = wx.Font(9, wx.FONTFAMILY_DEFAULT, wx.FONTSTYLE_NORMAL, wx.FONTWEIGHT_BOLD)
                dc.SetFont(name_font)
            dc.SetTextForeground(self.DANGER if is_warn else self.TEXT_MAIN)
            dc.DrawText(name, name_x,
                        y + (self.ROW_H - dc.GetTextExtent(name).GetHeight()) // 2)

            # 电量 + 状态点
            if pct_str:
                dc.SetFont(font_bold)
                dc.SetTextForeground(self.TEXT_MAIN)
                ext = dc.GetTextExtent(pct_str)
                pct_x = status_rect[0] + status_rect[2] - ext.GetWidth() - 18
                dc.DrawText(pct_str, pct_x, y + (self.ROW_H - ext.GetHeight()) // 2)
                # 状态圆点
                dc.SetBrush(wx.Brush(dot))
                dc.DrawCircle(status_rect[0] + status_rect[2] - 8, y + self.ROW_H // 2, 4)

            y += self.ROW_H

        # ── 分隔线 ──
        div_y = status_rect[1] + status_rect[3] + 9
        dc.SetPen(wx.Pen(self.DIVIDER, 1))
        dc.DrawLine(self.PAD, div_y, w - self.PAD, div_y)

        # ── 底部双按钮 ──
        btn_y = div_y + 5
        btn_h = self.BTN_H - 8
        half_w = (w - 2 * self.PAD) // 2

        for i, (label, fg, hover) in enumerate([
            ("打开控制中心", self.ACCENT, self.HOVER),
            ("退出", self.DANGER, self.HOVER),
        ]):
            x0 = self.PAD + i * half_w
            btn_w = half_w - 4
            btn_rect = wx.Rect(x0 + 2, btn_y, btn_w, btn_h)
            if self._hover_btn == i:
                dc.SetBrush(wx.Brush(self.HOVER))
                dc.SetPen(wx.TRANSPARENT_PEN)
                dc.DrawRoundedRectangle(btn_rect, 6)
            dc.SetFont(font_bold if i == 0 else font_name)
            dc.SetTextForeground(fg)
            ext = dc.GetTextExtent(label)
            tx = x0 + 2 + max(0, (btn_w - ext.GetWidth()) // 2)
            ty = btn_y + (btn_h - ext.GetHeight()) // 2
            dc.DrawText(label, tx, ty)

    # ── 交互 ──
    def _btn_hit(self, pos) -> int:
        w = self.GetSize().GetWidth()
        div_y = self.PAD + len(self._devices) * self.ROW_H + 8 + 9
        btn_y = div_y + 5
        btn_h = self.BTN_H - 8
        if btn_y <= pos.y <= btn_y + btn_h and self.PAD <= pos.x <= w - self.PAD:
            half_w = (w - 2 * self.PAD) // 2
            return 0 if pos.x < self.PAD + half_w else 1
        return -1

    def _on_motion(self, evt):
        self._set_hover(self._btn_hit(evt.GetPosition()))

    def _set_hover(self, idx: int):
        if idx != self._hover_btn:
            self._hover_btn = idx
            self.Refresh()

    def _on_click(self, evt):
        idx = self._btn_hit(evt.GetPosition())
        if idx < 0:
            return
        self.dismiss()
        cb_key = "dashboard" if idx == 0 else "exit"
        cb = self._cb.get(cb_key)
        if cb:
            wx.CallAfter(cb)

    def _on_key(self, evt):
        if evt.GetKeyCode() == wx.WXK_ESCAPE:
            self.dismiss()
        else:
            evt.Skip()

    def _on_activate(self, evt):
        # 弹窗失去激活（点击外部）时自动关闭
        if not evt.GetActive():
            self.dismiss()
        evt.Skip()

    def PositionAtCursor(self):
        mx, my = wx.GetMousePosition()
        w, h = self.GetSize()
        disp = wx.Display.GetFromPoint((mx, my))
        rect = wx.Display(disp if disp != wx.NOT_FOUND else 0).GetClientArea()
        x = mx - w + 8
        y = my - h - 4
        x = max(rect.x + 4, min(x, rect.x + rect.width - w - 4))
        y = max(rect.y + 4, min(y, rect.y + rect.height - h - 4))
        self.Move(x, y)


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
        # 已在显示则不重复创建
        if self._popup is not None and self._popup.IsShown():
            return
        if self._popup is not None:
            try:
                self._popup.Destroy()
            except Exception:
                pass
            self._popup = None
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
