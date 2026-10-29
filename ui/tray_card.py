"""
托盘右键卡片（重绘版）

设计规格（DIP，运行时按 DPI 缩放）：
  宽 396 · 内边距 16 · 标题行 30 · 设备行 64 · 底部按钮 36
  - 每台设备：44×44 圆角产品缩略图 + 名称 + 状态行 + 右对齐大号电量 + 贯通电量条
  - 底部：主按钮（品牌蓝实心）+ 次按钮（白底描边）
  - 全部走 wx.GraphicsContext 抗锯齿绘制；不用 emoji 当图标

为什么单独成模块：
旧实现在 ui/tray.py 里堆了 250 行 PaintDC 代码，画出来是「几行字 + 两个纯文字按钮」，
既没有产品图也没有电量条，视觉上跟系统右键菜单没差别。这里独立出来便于迭代。
"""
import os
import time
import ctypes
from typing import Dict, Callable, Optional, Tuple

import wx
from PIL import Image

from config import config, RESOURCE_DIR

# ───────────────────── 设计 token（唯一色彩来源） ─────────────────────
CARD_BG      = wx.Colour(0xFF, 0xFF, 0xFF)
BORDER       = wx.Colour(0xE4, 0xE9, 0xEF)
TEXT_MAIN    = wx.Colour(0x0F, 0x17, 0x2A)
TEXT_SUB     = wx.Colour(0x64, 0x74, 0x8B)
TEXT_FAINT   = wx.Colour(0x94, 0xA3, 0xB8)
TRACK        = wx.Colour(0xED, 0xF1, 0xF5)
ACCENT       = wx.Colour(0x00, 0x53, 0xE2)
ACCENT_HOVER = wx.Colour(0x00, 0x43, 0xB8)
ACCENT_SOFT  = wx.Colour(0xEB, 0xF3, 0xFF)
GHOST_HOVER  = wx.Colour(0xF8, 0xFA, 0xFC)
GHOST_BORDER = wx.Colour(0xE2, 0xE8, 0xF0)
GHOST_BORDER_HOVER = wx.Colour(0xCB, 0xD5, 0xE1)
DIVIDER      = wx.Colour(0xF1, 0xF5, 0xF9)
DANGER       = wx.Colour(0xDC, 0x26, 0x26)
WARN         = wx.Colour(0xD9, 0x77, 0x06)
OK_GREEN     = wx.Colour(0x05, 0x96, 0x69)

FONT_FACE = "Microsoft YaHei UI"
NUM_FACE = "Segoe UI"

# 产品缩略图缓存：(path, w, h) -> wx.Bitmap
_THUMB_CACHE: Dict[Tuple[str, int, int], wx.Bitmap] = {}


def _make_contain_thumb(path: str, w_px: int, h_px: int) -> Optional[wx.Bitmap]:
    """按 alpha 实际内容裁边 → 等比缩放到框内 → 居中补透明。

    为什么不用几何中心裁正方形：键盘图是 1071×386 的宽幅、鼠标图是 442×800 的
    竖幅，硬裁正方形会把鼠标裁得只剩"头"，键盘只剩中间几颗键帽。
    先按 alpha 边界裁掉四周空白，再等比 contain，两个设备都能看全。
    """
    key = (path, w_px, h_px)
    if key in _THUMB_CACHE:
        return _THUMB_CACHE[key]
    if not os.path.exists(path):
        return None
    try:
        im = Image.open(path).convert("RGBA")
        bbox = im.getbbox()
        if bbox:
            im = im.crop(bbox)
        im.thumbnail((w_px, h_px), Image.LANCZOS)
        canvas = Image.new("RGBA", (w_px, h_px), (0, 0, 0, 0))
        canvas.paste(im, ((w_px - im.width) // 2, (h_px - im.height) // 2), im)
        bmp = wx.Bitmap.FromBufferRGBA(w_px, h_px, canvas.tobytes())
        _THUMB_CACHE[key] = bmp
        return bmp
    except Exception as e:
        print(f"[TrayCard] 生成缩略图失败 {path}: {e}")
        return None


def _battery_tone(online: bool, pct: int, charging: bool) -> wx.Colour:
    if not online or pct <= 0:
        return wx.Colour(0xCB, 0xD5, 0xE1)
    if charging:
        return ACCENT
    if pct <= 10:
        return DANGER
    if pct <= int(config.get("warning_threshold", 20) or 20):
        return WARN
    return ACCENT


def _status_text(st) -> str:
    online = bool(getattr(st, "online", False))
    pct = int(getattr(st, "percent", -1))
    raw = getattr(st, "raw_info", "") or ""
    if raw == "Sleeping":
        return "休眠中 · 移动可唤醒"
    if not online:
        return "未连接"
    if pct <= 0:
        return "等待硬件响应"
    if getattr(st, "charging", False):
        return "正在充电"
    if "Cached" in raw:
        return "上次读数 · 待刷新"
    return "2.4G 已连接"


class TrayCardPopup(wx.Frame):
    """轻量状态卡片：设备概览 + 两个动作按钮。"""

    WIDTH = 396
    PAD = 16
    HEADER_H = 30
    ROW_H = 64
    BTN_H = 36
    THUMB_W = 52
    THUMB_H = 40

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
        self._rows = []          # [(name, pct, charging, online, status, tone, thumb_path)]
        self._extra = 0

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
        self._rows = []
        statuses = self.engine.get_all_statuses()
        # 显示的排序：键盘在前、鼠标在后（与主界面侧栏一致）
        ordered = sorted(statuses.values(),
                         key=lambda s: 0 if getattr(s, "device_type", "") != "mouse" else 1)
        head = ordered[:4]
        self._extra = max(0, len(ordered) - len(head))
        for st in head:
            pct = int(getattr(st, "percent", -1))
            online = bool(getattr(st, "online", False))
            is_kb = getattr(st, "device_type", "mouse") != "mouse"
            self._rows.append({
                "name": getattr(st, "device_name", "未知外设"),
                "pct": pct,
                "charging": bool(getattr(st, "charging", False)),
                "online": online,
                "status": _status_text(st),
                "tone": _battery_tone(online, pct, bool(getattr(st, "charging", False))),
                "img": os.path.join(RESOURCE_DIR, "ui", "assets",
                                    "dev_keyboard.png" if is_kb else "dev_mouse.png"),
            })
        if not self._rows:
            self._rows.append({
                "name": "未检测到外设", "pct": -1, "charging": False, "online": False,
                "status": "插入 2.4G 接收器后自动识别", "tone": TRACK, "img": "",
            })

    def _calc_height(self) -> int:
        n = len(self._rows)
        return (self.PAD + self.HEADER_H + 6 + n * self.ROW_H
                + 12 + 1 + 12 + self.BTN_H + self.PAD)

    # ─────────────── 几何 ───────────────

    def _btn_rects(self) -> Tuple[wx.Rect, wx.Rect]:
        d = self.FromDIP
        w = self.GetClientSize().width
        y = d(self.PAD + self.HEADER_H + 6
              + len(self._rows) * self.ROW_H + 12 + 1 + 12)
        gap = d(10)
        inner = w - d(self.PAD) * 2
        main_w = int((inner - gap) * 0.58)
        ghost_w = inner - gap - main_w
        r1 = wx.Rect(d(self.PAD), y, main_w, d(self.BTN_H))
        r2 = wx.Rect(d(self.PAD) + main_w + gap, y, ghost_w, d(self.BTN_H))
        return r1, r2

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
        # 缩略图缩放质量：常量名在 wx 各版本间有差异，取不到就用默认
        for name in ("INTERPOLATION_BEST", "INTERPOLATION_GOOD", "INTERPOLATION_HIGH"):
            mode = getattr(wx, name, None)
            if mode is not None:
                try:
                    gc.SetInterpolationQuality(mode)
                except Exception:
                    pass
                break

        d = self.FromDIP
        w, h = self.GetClientSize()

        # 外描边：让卡片与桌面之间有一条清晰的分界
        gc.SetPen(wx.Pen(BORDER, 1))
        gc.SetBrush(wx.TRANSPARENT_BRUSH)
        gc.DrawRoundedRectangle(0.5, 0.5, w - 1, h - 1, d(10))

        # ── 标题行 ──
        gc.SetFont(self._font(10.5, True), TEXT_MAIN)
        gc.DrawText("外设电量", d(self.PAD), d(self.PAD + 4))

        tag = "2.4G 无线"
        gc.SetFont(self._font(8), TEXT_FAINT)
        tw, th = gc.GetTextExtent(tag)
        gc.DrawText(tag, w - d(self.PAD) - tw, d(self.PAD + 7))

        # ── 设备行 ──
        y = d(self.PAD + self.HEADER_H + 6)
        for i, row in enumerate(self._rows):
            self._paint_row(gc, row, y, w, i)
            y += d(self.ROW_H)

        # ── 分隔线 ──
        line_y = y + d(12)
        gc.SetPen(wx.Pen(DIVIDER, 1))
        gc.StrokeLine(d(self.PAD), line_y, w - d(self.PAD), line_y)

        # ── 底部按钮 ──
        r1, r2 = self._btn_rects()
        self._paint_button(gc, r1, "打开控制中心", primary=True, hovered=self._hover_btn == 0)
        self._paint_button(gc, r2, "退出 Volt", primary=False, hovered=self._hover_btn == 1)

    def _paint_row(self, gc, row, y, w, idx):
        d = self.FromDIP
        pad = d(self.PAD)
        box_w = d(self.THUMB_W)
        box_h = d(self.THUMB_H)
        box_y = y + (d(self.ROW_H) - box_h) / 2.0

        # 缩略图底板（浅灰圆角），产品图等比叠在上面
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.SetBrush(wx.Brush(wx.Colour(0xF5, 0xF7, 0xFA)))
        gc.DrawRoundedRectangle(pad, box_y, box_w, box_h, d(9))

        scale = self.GetDPIScaleFactor()
        thumb = (_make_contain_thumb(row["img"], int(box_w * scale), int(box_h * scale))
                 if row["img"] else None)
        if thumb is not None:
            gc.DrawBitmap(thumb, pad, box_y, box_w, box_h)

        text_x = pad + box_w + d(12)

        # 设备名
        gc.SetFont(self._font(10, True), TEXT_MAIN)
        name = row["name"]
        max_w = w - d(self.PAD) - text_x - d(96)
        while gc.GetTextExtent(name)[0] > max_w and len(name) > 4:
            name = name[:-2] + "…"
        gc.DrawText(name, text_x, y + d(8))

        # 状态行：小圆点 + 文案
        dot_r = d(3)
        dot_cx = text_x + dot_r
        dot_cy = y + d(32)
        gc.SetBrush(wx.Brush(row["tone"] if row["online"] else TEXT_FAINT))
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.DrawEllipse(dot_cx - dot_r, dot_cy - dot_r, dot_r * 2, dot_r * 2)

        gc.SetFont(self._font(8), TEXT_SUB)
        gc.DrawText(row["status"], text_x + dot_r * 2 + d(6), y + d(26))

        # 电量数字（右对齐，等宽观感）
        # 用"总宽 + 统一左起点"排版：分别右对齐数字和 % 会让 % 溢出卡片右缘
        right = w - d(self.PAD)
        if row["online"] and row["pct"] > 0:
            num = str(row["pct"])
            f_num = self._font(17, True, NUM_FACE)
            f_pct = self._font(9, True, NUM_FACE)

            gc.SetFont(f_num, row["tone"])
            nw, nh = gc.GetTextExtent(num)
            gc.SetFont(f_pct, row["tone"])
            pw, ph = gc.GetTextExtent("%")

            x0 = right - nw - pw
            gc.SetFont(f_num, row["tone"])
            gc.DrawText(num, x0, y + d(6))
            gc.SetFont(f_pct, row["tone"])
            gc.DrawText("%", x0 + nw + d(1), y + d(6) + nh - ph - d(3))
        else:
            f_dim = self._font(13, True, NUM_FACE)
            gc.SetFont(f_dim, wx.Colour(0xCB, 0xD5, 0xE1))
            tw, _ = gc.GetTextExtent("--")
            gc.DrawText("--", right - tw, y + d(10))

        # 电量条
        bar_x = text_x
        bar_y = y + d(48)
        bar_w = w - d(self.PAD) - bar_x
        bar_h = d(4)
        gc.SetPen(wx.TRANSPARENT_PEN)
        gc.SetBrush(wx.Brush(TRACK))
        gc.DrawRoundedRectangle(bar_x, bar_y, bar_w, bar_h, bar_h / 2.0)
        if row["online"] and row["pct"] > 0:
            fill_w = max(bar_h, bar_w * min(100, row["pct"]) / 100.0)
            gc.SetBrush(wx.Brush(row["tone"]))
            gc.DrawRoundedRectangle(bar_x, bar_y, fill_w, bar_h, bar_h / 2.0)

        # 行间细分割（最后一行的线由底部大分隔线负责）
        if idx < len(self._rows) - 1:
            gc.SetPen(wx.Pen(DIVIDER, 1))
            gc.StrokeLine(text_x, y + d(self.ROW_H) - 1, w - d(self.PAD), y + d(self.ROW_H) - 1)

    def _paint_button(self, gc, rect: wx.Rect, label: str, primary: bool, hovered: bool):
        d = self.FromDIP
        radius = d(8)
        if primary:
            fill = ACCENT_HOVER if hovered else ACCENT
            pen = wx.TRANSPARENT_PEN
            fg = wx.Colour(0xFF, 0xFF, 0xFF)
        else:
            fill = GHOST_HOVER if hovered else CARD_BG
            pen = wx.Pen(GHOST_BORDER_HOVER if hovered else GHOST_BORDER, 1)
            fg = DANGER if hovered else TEXT_SUB

        gc.SetPen(pen)
        gc.SetBrush(wx.Brush(fill))
        gc.DrawRoundedRectangle(rect.x, rect.y, rect.width, rect.height, radius)

        gc.SetFont(self._font(9, primary), fg)
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
        """Windows 11 系统圆角；Win10 上静默失败（退回直角，不影响可用性）"""
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
        print(f"[TrayCard] 已弹出，{len(self._rows)} 台设备", flush=True)

    def PositionAtCursor(self):
        mx, my = wx.GetMousePosition()
        w, h = self.GetSize()
        disp = wx.Display.GetFromPoint((mx, my))
        rect = wx.Display(disp if disp != wx.NOT_FOUND else 0).GetClientArea()
        # 默认贴在光标左下方（托盘在右下角，卡片往左上方展开更自然）
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
