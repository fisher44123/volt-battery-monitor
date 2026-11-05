"""
托盘右键卡片 —— 快览型，不是产品册

设计原则（这一版推翻了上一版）：
  卡片的唯一意义是「不打开主窗口，一眼看到两台设备的电量，顺手点个动作」。
  所以：
    · 不要产品图       —— 占 40px 宽度只为装饰，且 42px 的缩略图根本看不清型号
    · 不要标题栏       —— 右键托盘弹出来的东西，用户已经知道这是什么
    · 不要常驻状态文案 —— "2.4G 已连接" 属于废话，只在休眠/离线这种异常态才提示
    · 信息密度优先     —— 一行一台设备：名字 + 右对齐电量 + 一条 2px 比例条

尺寸：244 × (53 + 32×设备数) DIP。两台设备 = 117 高。
"""
import time
import ctypes
import ctypes.wintypes as wt
from typing import Dict, Callable, Optional

import wx

from config import config

# ───────────────────── 设计 token ─────────────────────
CARD_BG      = wx.Colour(0xFF, 0xFF, 0xFF)
BORDER       = wx.Colour(0xE1, 0xE7, 0xEE)
TEXT_MAIN    = wx.Colour(0x0F, 0x17, 0x2A)
TEXT_SUB     = wx.Colour(0x64, 0x74, 0x8B)
TEXT_FAINT   = wx.Colour(0x9A, 0xA6, 0xB6)
TRACK        = wx.Colour(0xEC, 0xF1, 0xF6)
ACCENT       = wx.Colour(0x00, 0x53, 0xE2)
ACCENT_HOVER = wx.Colour(0x00, 0x43, 0xB8)
ACCENT_SOFT  = wx.Colour(0xE9, 0xF1, 0xFF)   # 主按钮常态底色：不与电量数字抢视觉焦点
GHOST_HOVER  = wx.Colour(0xF6, 0xF8, 0xFB)
GHOST_BORDER = wx.Colour(0xE4, 0xE9, 0xEF)
DIVIDER      = wx.Colour(0xF0, 0xF4, 0xF8)
DANGER       = wx.Colour(0xDC, 0x26, 0x26)
WARN         = wx.Colour(0xD9, 0x77, 0x06)
OK_GREEN     = wx.Colour(0x05, 0x96, 0x69)

FONT_FACE = "Microsoft YaHei UI"
NUM_FACE = "Segoe UI"


def _taskbar_geometry():
    """用 Windows AppBar API 拿到真实任务栏位置、边位与自动隐藏状态。

    吸收自 myfinder-device-battery 的 issue #61：任务栏设为「自动隐藏」时，
    只用 wx.Display.GetClientArea() 算位置会让面板底栏被任务栏压住。
    返回 (edge, left, top, right, bottom, autohide)，失败返回 None。
    """
    class APPBARDATA(ctypes.Structure):
        _fields_ = [
            ("cbSize", wt.DWORD), ("hWnd", wt.HWND),
            ("uCallbackMessage", wt.UINT), ("uEdge", wt.UINT),
            ("rc", wt.RECT), ("lParam", ctypes.c_ssize_t),
        ]

    try:
        shell32 = ctypes.windll.shell32
        shell32.SHAppBarMessage.argtypes = [wt.DWORD, ctypes.POINTER(APPBARDATA)]
        shell32.SHAppBarMessage.restype = ctypes.c_ssize_t
        abd = APPBARDATA()
        abd.cbSize = ctypes.sizeof(abd)
        ABM_GETSTATE, ABM_GETTASKBARPOS, ABS_AUTOHIDE = 4, 5, 0x1
        if not shell32.SHAppBarMessage(ABM_GETTASKBARPOS, ctypes.byref(abd)):
            return None
        state = shell32.SHAppBarMessage(ABM_GETSTATE, ctypes.byref(abd))
        r = abd.rc
        return (abd.uEdge, r.left, r.top, r.right, r.bottom, bool(state & ABS_AUTOHIDE))
    except Exception:
        return None


def _tone(online: bool, pct: int, charging: bool) -> wx.Colour:
    if not online or pct <= 0:
        return TEXT_FAINT
    if charging:
        return OK_GREEN
    if pct <= 10:
        return DANGER
    if pct <= int(config.get("warning_threshold", 20) or 20):
        return WARN
    return ACCENT


def _flag_text(st) -> str:
    """只在异常态给一句提示；正常态返回空串，不占任何视觉噪音。"""
    raw = getattr(st, "raw_info", "") or ""
    if raw == "Sleeping":
        return "休眠"
    if not bool(getattr(st, "online", False)):
        return "未连接"
    if int(getattr(st, "percent", -1)) <= 0:
        return "待同步"
    return ""


class TrayCardPopup(wx.Frame):
    WIDTH = 244
    PAD = 10
    ROW_H = 30
    BTN_H = 26
    GAP_ROW_END = 6      # 末行 → 分隔线
    GAP_LINE_BTN = 6     # 分隔线 → 按钮

    def __init__(self, engine, callbacks: Dict[str, Callable]):
        app = wx.GetApp()
        parent = None
        if app is not None:
            top = app.GetTopWindow()
            parent = top if top is not None else getattr(app, "_anchor", None)
        if parent is None:
            parent = wx.Frame(None, size=(1, 1), style=wx.NO_BORDER)

        super().__init__(parent, title="Volt",
                         style=wx.FRAME_NO_TASKBAR | wx.NO_BORDER | wx.STAY_ON_TOP)
        self.engine = engine
        self._cb = callbacks
        self._hover_btn = -1
        self._closing = False
        self._shown_at = 0.0
        self._rows = []

        self.SetBackgroundColour(CARD_BG)
        self.SetDoubleBuffered(True)
        self._build_rows()
        self.SetSize(self.FromDIP(wx.Size(self.WIDTH, self._calc_height())))

        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_LEFT_DOWN, self._on_click)
        self.Bind(wx.EVT_MOTION, self._on_motion)
        self.Bind(wx.EVT_LEAVE_WINDOW, lambda e: self._set_hover(-1))
        self.Bind(wx.EVT_KEY_DOWN, self._on_key)
        self.Bind(wx.EVT_ACTIVATE, self._on_activate)
        self._apply_system_round_corner()

    # ─────────────── 数据 ───────────────

    def _build_rows(self):
        statuses = self.engine.get_all_statuses()
        ordered = sorted(statuses.values(),
                         key=lambda s: 0 if getattr(s, "device_type", "") != "mouse" else 1)
        for st in ordered[:4]:
            pct = int(getattr(st, "percent", -1))
            online = bool(getattr(st, "online", False))
            charging = bool(getattr(st, "charging", False))
            # 缓存值 / 超 90 秒的历史读数用灰色，不冒充当前在线
            # （置灰只通过 tone 传导到 _paint_row，不单独存 muted 标记）
            muted = not getattr(st, "is_trustworthy", True)
            self._rows.append({
                "name": getattr(st, "device_name", "未知外设"),
                "pct": pct,
                "online": online,
                "charging": charging,
                "flag": _flag_text(st),
                "tone": TEXT_FAINT if muted else _tone(online, pct, charging),
            })
        if not self._rows:
            self._rows.append({
                "name": "未检测到外设", "pct": -1, "online": False,
                "charging": False, "flag": "插入接收器后自动识别", "tone": TEXT_FAINT,
            })

    def _calc_height(self) -> int:
        n = len(self._rows)
        return (self.PAD * 2 + n * self.ROW_H
                + self.GAP_ROW_END + 1 + self.GAP_LINE_BTN + self.BTN_H)

    # ─────────────── 几何 ───────────────

    def _btn_rects(self):
        d = self.FromDIP
        w = self.GetClientSize().width
        y = d(self.PAD + len(self._rows) * self.ROW_H + self.GAP_ROW_END + 1 + self.GAP_LINE_BTN)
        gap = d(6)
        inner = w - d(self.PAD) * 2
        main_w = int((inner - gap) * 0.6)
        ghost_w = inner - gap - main_w
        return (wx.Rect(d(self.PAD), y, main_w, d(self.BTN_H)),
                wx.Rect(d(self.PAD) + main_w + gap, y, ghost_w, d(self.BTN_H)))

    # ─────────────── 绘制 ───────────────

    def _font(self, pt: float, bold: bool = False, face: str = FONT_FACE) -> wx.Font:
        info = wx.FontInfo(pt).FaceName(face)
        info = info.Weight(wx.FONTWEIGHT_BOLD if bold else wx.FONTWEIGHT_NORMAL)
        return wx.Font(info)

    def _on_paint(self, evt):
        dc = wx.PaintDC(self)
        dc.SetBackground(wx.Brush(CARD_BG))
        dc.Clear()

        gc = wx.GraphicsContext.Create(dc)
        if gc is None:
            return
        gc.SetAntialiasMode(wx.ANTIALIAS_DEFAULT)

        d = self.FromDIP
        w, h = self.GetClientSize()

        gc.SetPen(wx.Pen(BORDER, 1))
        gc.SetBrush(wx.TRANSPARENT_BRUSH)
        gc.DrawRoundedRectangle(0.5, 0.5, w - 1, h - 1, d(8))

        y = d(self.PAD)
        for i, row in enumerate(self._rows):
            self._paint_row(gc, row, y, w, i)
            y += d(self.ROW_H)

        line_y = y + d(self.GAP_ROW_END)
        gc.SetPen(wx.Pen(DIVIDER, 1))
        gc.StrokeLine(d(self.PAD), line_y, w - d(self.PAD), line_y)

        r1, r2 = self._btn_rects()
        self._paint_button(gc, r1, "打开控制中心", primary=True, hovered=self._hover_btn == 0)
        self._paint_button(gc, r2, "退出", primary=False, hovered=self._hover_btn == 1)

    def _paint_row(self, gc, row, y, w, idx):
        d = self.FromDIP
        left = d(self.PAD)
        right = w - d(self.PAD)

        # 名字与右侧数字共用同一条视觉中线（此前一个贴顶、一个贴底，
        # 看起来像两个没对齐的独立元素）
        name_y = y + d(2)
        gc.SetFont(self._font(9, True), TEXT_MAIN)
        name = row["name"]
        max_w = w - d(self.PAD) * 2 - d(54)
        while gc.GetTextExtent(name)[0] > max_w and len(name) > 4:
            name = name[:-2] + "…"
        nw, nh = gc.GetTextExtent(name)
        gc.DrawText(name, left, name_y)

        if row["flag"]:
            gc.SetFont(self._font(9), TEXT_FAINT)
            fw, fh = gc.GetTextExtent(row["flag"])
            gc.DrawText(row["flag"], right - fw, name_y + (nh - fh) / 2.0)
        elif row["pct"] > 0:
            num = str(row["pct"])
            f_num = self._font(11, True, NUM_FACE)
            f_pct = self._font(7, True, NUM_FACE)

            gc.SetFont(f_num, row["tone"])
            gw, gh = gc.GetTextExtent(num)
            gc.SetFont(f_pct, row["tone"])
            pw, ph = gc.GetTextExtent("%")

            x0 = right - gw - pw
            num_y = name_y + (nh - gh) / 2.0
            gc.SetFont(f_num, row["tone"])
            gc.DrawText(num, x0, num_y)
            gc.SetFont(f_pct, row["tone"])
            gc.DrawText("%", x0 + gw + d(1), num_y + gh - ph - d(2))
        else:
            gc.SetFont(self._font(10, True, NUM_FACE), TEXT_FAINT)
            gw, gh = gc.GetTextExtent("--")
            gc.DrawText("--", right - gw, name_y + (nh - gh) / 2.0)

        # 2px 比例条：把百分比变成可一眼比较的长度
        bar_y = y + d(22)
        bar_h = d(2)
        bar_w = right - left
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.SetBrush(wx.Brush(TRACK))
        gc.DrawRoundedRectangle(left, bar_y, bar_w, bar_h, bar_h / 2.0)
        if row["online"] and row["pct"] > 0:
            fill = max(bar_h, bar_w * min(100, row["pct"]) / 100.0)
            gc.SetBrush(wx.Brush(row["tone"]))
            gc.DrawRoundedRectangle(left, bar_y, fill, bar_h, bar_h / 2.0)

        # 行间不需要分割线：细条本身已经是视觉分隔，多一条线只显得碎
        _ = idx

    def _paint_button(self, gc, rect: wx.Rect, label: str, primary: bool, hovered: bool):
        d = self.FromDIP
        if primary:
            # 常态浅蓝、hover 才变实心：卡片的视觉主角应该是电量数字，不是按钮
            fill = ACCENT if hovered else ACCENT_SOFT
            pen = wx.TRANSPARENT_PEN
            fg = wx.Colour(0xFF, 0xFF, 0xFF) if hovered else ACCENT
        else:
            fill = GHOST_HOVER if hovered else CARD_BG
            pen = wx.Pen(GHOST_BORDER, 1)
            fg = DANGER if hovered else TEXT_SUB

        gc.SetPen(pen)
        gc.SetBrush(wx.Brush(fill))
        gc.DrawRoundedRectangle(rect.x, rect.y, rect.width, rect.height, d(6))

        gc.SetFont(self._font(8.5, primary), fg)
        tw, th = gc.GetTextExtent(label)
        gc.DrawText(label,
                    rect.x + (rect.width - tw) / 2.0,
                    rect.y + (rect.height - th) / 2.0)

    # ─────────────── 交互 ───────────────

    def _hit(self, pos) -> int:
        r1, r2 = self._btn_rects()
        if r1.Contains(pos):
            return 0
        if r2.Contains(pos):
            return 1
        return -1

    def _on_motion(self, evt):
        idx = self._hit(evt.GetPosition())
        if idx != self._hover_btn:
            self._hover_btn = idx
            self.SetCursor(wx.Cursor(wx.CURSOR_HAND if idx >= 0 else wx.CURSOR_ARROW))
            self.Refresh()

    def _set_hover(self, idx: int):
        if idx != self._hover_btn:
            self._hover_btn = idx
            self.SetCursor(wx.Cursor(wx.CURSOR_ARROW))
            self.Refresh()

    def _on_click(self, evt):
        idx = self._hit(evt.GetPosition())
        if idx < 0:
            # 点设备行 = 打开控制中心（卡片整体也是一个"去看看"的入口）
            y = evt.GetPosition().y
            if y < self.FromDIP(self.PAD + len(self._rows) * self.ROW_H):
                self.dismiss()
                cb = self._cb.get("dashboard")
                if cb:
                    wx.CallAfter(cb)
            return
        self.dismiss()
        cb = self._cb.get("dashboard" if idx == 0 else "exit")
        if cb:
            wx.CallAfter(cb)

    def _on_key(self, evt):
        if evt.GetKeyCode() == wx.WXK_ESCAPE:
            self.dismiss()
        else:
            evt.Skip()

    def _on_activate(self, evt):
        # 刚弹出的 250ms 内不判定失活：从托盘右键到窗口真正拿到焦点的路上，
        # Windows 会先发一次 EVT_ACTIVATE(False)，直接关掉会表现为"右键一闪就没"。
        if not evt.GetActive() and (time.time() - self._shown_at) > 0.25:
            self.dismiss()
        evt.Skip()

    # ─────────────── 显示 / 关闭 ───────────────

    def _apply_system_round_corner(self):
        try:
            hwnd = self.GetHandle()
            pref = ctypes.c_int(2)      # DWMWCP_ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref))
        except Exception:
            pass

    def show_at_cursor(self):
        self._shown_at = time.time()
        self.PositionAtCursor()
        self.Show()
        self.Raise()
        try:
            self.SetFocus()
        except Exception:
            pass
        # wx.Size 的 width / height 是属性，不是方法（写成 .height() 会抛
        # "'int' object is not callable"，而这行在 show_at_cursor 里，
        # 等于右键直接炸掉、卡片根本不显示）
        sz = self.GetSize()
        print(f"[TrayCard] 已弹出，{len(self._rows)} 台设备，"
              f"尺寸 {sz.width}x{sz.height}", flush=True)

    def PositionAtCursor(self):
        mx, my = wx.GetMousePosition()
        w, h = self.GetSize()
        disp = wx.Display.GetFromPoint((mx, my))
        rect = wx.Display(disp if disp != wx.NOT_FOUND else 0).GetClientArea()
        margin = 8

        tb = _taskbar_geometry()
        if tb:
            edge, l, t, r, b, autohide = tb
            # ABE_LEFT=0 / TOP=1 / RIGHT=2 / BOTTOM=3
            if autohide:
                # 自动隐藏时工作区包含原任务栏区域，贴工作区边缘并留 8px，
                # 避免卡片压到弹出时的任务栏（对方 issue #61 的坑）
                x = (rect.x + margin if edge == 0
                     else rect.x + rect.width - w - margin)
                y = (rect.y + margin if edge == 1
                     else rect.y + rect.height - h - margin)
            elif edge == 3:       # 底部
                x, y = r - w - margin, t - h - margin
            elif edge == 1:       # 顶部
                x, y = r - w - margin, b + margin
            elif edge == 0:       # 左侧
                x, y = r + margin, rect.y + rect.height - h - margin
            else:                 # 右侧
                x, y = l - w - margin, rect.y + rect.height - h - margin

            x = max(rect.x + margin, min(x, rect.x + rect.width - w - margin))
            y = max(rect.y + margin, min(y, rect.y + rect.height - h - margin))
            self.Move(x, y)
            return

        # 兜底：拿不到 AppBar 信息时用光标左上方
        x = mx - w + 8
        y = my - h - 8
        x = max(rect.x + 8, min(x, rect.x + rect.width - w - 8))
        y = max(rect.y + 8, min(y, rect.y + rect.height - h - 8))
        self.Move(x, y)

    def dismiss(self):
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
