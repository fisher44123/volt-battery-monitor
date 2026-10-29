"""
纯 Win32 原生 OSD 悬浮胶囊窗 (ctypes)
1:1 像素级复刻 rapoo-tray (osd_window.cpp & osd.png)
- 尺寸: 310 x 94 (DPI 自适应)
- 背景: RGB(22, 25, 34), 边框 RGB(70, 78, 96), 圆角 16px
- Line 1: 大粗体 18px 白色 (RGB 255, 255, 255)
- Line 2: 属性中号 13px 浅灰蓝 (RGB 210, 222, 238)
- Line 3: 状态青色 12px 高亮 (RGB 120, 210, 255)
- 动画: 快速平滑淡入 -> 停留 3.5 秒 -> 平滑淡出消失
"""
import ctypes
import ctypes.wintypes as wt
import threading
import time
from typing import Dict, List, Optional
from core.battery import BatteryStatus

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000

LWA_COLORKEY = 1
LWA_ALPHA = 2
COLOR_KEY = 0x00FF00FF  # RGB(255, 0, 255) 品红镂空 Key

class RECT(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG), ("right", wt.LONG), ("bottom", wt.LONG)]

class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", wt.HDC),
        ("fErase", wt.BOOL),
        ("rcPaint", RECT),
        ("fRestore", wt.BOOL),
        ("fIncUpdate", wt.BOOL),
        ("rgbReserved", wt.BYTE * 32)
    ]

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_long, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.UINT),
        ("style", wt.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE),
        ("hIcon", wt.HICON),
        ("hCursor", wt.HICON),
        ("hbrBackground", wt.HBRUSH),
        ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR),
        ("hIconSm", wt.HICON)
    ]

def _scale_dpi(val: int, dpi: int) -> int:
    return int(val * dpi / 96)

def _get_dpi_for_window(hwnd) -> int:
    try:
        fn = getattr(user32, "GetDpiForWindow", None)
        if fn and hwnd:
            d = fn(hwnd)
            if d > 0:
                return d
    except Exception:
        pass
    try:
        hdc = user32.GetDC(0)
        d = gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
        user32.ReleaseDC(0, hdc)
        if d > 0:
            return d
    except Exception:
        pass
    return 96


class NativeOsdManager:
    """1:1 复刻 rapoo-tray 原生 OSD 悬浮胶囊窗口"""
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(NativeOsdManager, cls).__new__(cls)
            cls._instance._inited = False
        return cls._instance

    def __init__(self):
        if getattr(self, "_inited", False):
            return
        self._inited = True
        self.hwnd = None
        self._lock = threading.Lock()
        self._line1 = "外设 2.4G 模式已连接"
        self._line2 = "凌豹 K98: 待同步  |  迈从 G3: 待同步"
        self._line3 = "2.4G 模式 | ⚡ 充放电监控 | 运行中"
        self._alpha = 0
        self._hide_deadline = 0.0
        self._anim_thread = None
        self._cls_name = "RapooOsdPopupWnd"
        self._wndproc_ref = WNDPROC(self._wnd_proc)

        self._create_window()

    def _create_window(self):
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = 3  # CS_HREDRAW | CS_VREDRAW
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = hinst
        wc.hCursor = user32.LoadCursorW(None, 32512)
        wc.lpszClassName = self._cls_name
        user32.RegisterClassExW(ctypes.byref(wc))

        dpi = _get_dpi_for_window(0)
        w = _scale_dpi(310, dpi)
        h = _scale_dpi(94, dpi)

        rc_work = RECT()
        user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rc_work), 0)
        x = rc_work.right - w - _scale_dpi(20, dpi)
        y = rc_work.bottom - h - _scale_dpi(20, dpi)

        self.hwnd = user32.CreateWindowExW(
            WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_NOACTIVATE,
            self._cls_name,
            "RapooOSD",
            WS_POPUP,
            x, y, w, h,
            None, None, hinst, None
        )

        user32.SetLayeredWindowAttributes(self.hwnd, COLOR_KEY, 0, LWA_COLORKEY | LWA_ALPHA)

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == 0x0014:
            return 1
        elif msg == 0x000F:
            self._do_paint(hwnd)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _do_paint(self, hwnd):
        ps = PAINTSTRUCT()
        hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
        rc = RECT()
        user32.GetClientRect(hwnd, ctypes.byref(rc))
        w = rc.right - rc.left
        h = rc.bottom - rc.top

        memdc = gdi32.CreateCompatibleDC(hdc)
        membmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        oldbmp = gdi32.SelectObject(memdc, membmp)

        dpi = _get_dpi_for_window(hwnd)
        S = lambda v: _scale_dpi(v, dpi)

        # 1. 品红 Key 背景用于完美无黑边镂空
        key_brush = gdi32.CreateSolidBrush(COLOR_KEY)
        user32.FillRect(memdc, ctypes.byref(rc), key_brush)
        gdi32.DeleteObject(key_brush)

        # 2. 1:1 对标 rapoo-tray osd.png：深色圆角卡片 RGB(22, 25, 34), 边框 RGB(70, 78, 96)
        rc_box = RECT(S(2), S(2), w - S(2), h - S(2))
        bg_brush = gdi32.CreateSolidBrush(0x00221916)  # RGB(22, 25, 34) (COLORREF: 0x00BBGGRR)
        border_pen = gdi32.CreatePen(0, 1, 0x00604E46)  # RGB(70, 78, 96)
        old_br = gdi32.SelectObject(memdc, bg_brush)
        old_pen = gdi32.SelectObject(memdc, border_pen)
        radius = S(16)
        gdi32.RoundRect(memdc, rc_box.left, rc_box.top, rc_box.right, rc_box.bottom, radius, radius)

        gdi32.SetBkMode(memdc, 1)  # TRANSPARENT

        with self._lock:
            line1 = self._line1
            line2 = self._line2
            line3 = self._line3

        # 3. Line 1: 大粗体标题 (18px, 白色 RGB 255, 255, 255)
        font1 = gdi32.CreateFontW(-S(18), 0, 0, 0, 700, False, False, False, 1, 0, 0, 5, 0, "Microsoft YaHei UI")
        old_font = gdi32.SelectObject(memdc, font1)
        gdi32.SetTextColor(memdc, 0x00FFFFFF)
        rc_top = RECT(rc_box.left + S(8), rc_box.top + S(8), rc_box.right - S(8), rc_box.top + S(34))
        user32.DrawTextW(memdc, line1, -1, ctypes.byref(rc_top), 1 | 4 | 32)  # DT_CENTER | DT_VCENTER | DT_SINGLELINE

        # 4. Line 2: 副标题明细 (13px, 浅灰蓝 RGB 210, 222, 238)
        font2 = gdi32.CreateFontW(-S(13), 0, 0, 0, 400, False, False, False, 1, 0, 0, 5, 0, "Microsoft YaHei UI")
        gdi32.SelectObject(memdc, font2)
        gdi32.SetTextColor(memdc, 0x00EED2D2)
        rc_mid = RECT(rc_box.left + S(8), rc_top.bottom + S(2), rc_box.right - S(8), rc_top.bottom + S(26))
        user32.DrawTextW(memdc, line2, -1, ctypes.byref(rc_mid), 1 | 4 | 32)

        # 5. Line 3: 底部高亮状态栏 (12px 半粗, 青色高亮 RGB 120, 210, 255)
        font3 = gdi32.CreateFontW(-S(12), 0, 0, 0, 600, False, False, False, 1, 0, 0, 5, 0, "Microsoft YaHei UI")
        gdi32.SelectObject(memdc, font3)
        gdi32.SetTextColor(memdc, 0x00FFD278)  # RGB(120, 210, 255)
        rc_bot = RECT(rc_box.left + S(8), rc_mid.bottom + S(1), rc_box.right - S(8), rc_box.bottom - S(8))
        user32.DrawTextW(memdc, line3, -1, ctypes.byref(rc_bot), 1 | 4 | 32)

        # 拷贝到屏幕
        gdi32.BitBlt(hdc, 0, 0, w, h, memdc, 0, 0, 0x00CC0020)

        # 清理 GDI
        gdi32.SelectObject(memdc, old_font)
        gdi32.SelectObject(memdc, old_pen)
        gdi32.SelectObject(memdc, old_br)
        gdi32.SelectObject(memdc, oldbmp)
        gdi32.DeleteObject(membmp)
        gdi32.DeleteDC(memdc)
        gdi32.DeleteObject(font1)
        gdi32.DeleteObject(font2)
        gdi32.DeleteObject(font3)
        gdi32.DeleteObject(border_pen)
        gdi32.DeleteObject(bg_brush)

        user32.EndPaint(hwnd, ctypes.byref(ps))

    def show(self, statuses: Dict[str, BatteryStatus]):
        """根据真实外设状态渲染 1:1 rapoo-tray 样式的 OSD"""
        k98 = statuses.get("lingbao_k98")
        g3 = statuses.get("mchose_g3v2")

        # 构造 Line 1 (大标题，高亮显示当前电量确凿的设备)
        if k98 and k98.percent > 0:
            chg = " ⚡" if k98.charging else ""
            l1 = f"凌豹 K98  {k98.percent}%{chg}"
        elif g3 and g3.percent > 0:
            chg = " ⚡" if g3.charging else ""
            l1 = f"迈从 G3  {g3.percent}%{chg}"
        else:
            l1 = "2.4G 外设已连接"

        # 构造 Line 2 (详细对照)
        parts = []
        if k98:
            kp = f"{k98.percent}%" if k98.percent > 0 else "待同步"
            parts.append(f"K98: {kp}")
        if g3:
            gp = f"{g3.percent}%" if g3.percent > 0 else "待同步"
            parts.append(f"G3: {gp}")
        l2 = "   |   ".join(parts) if parts else "正在等待外设接入..."

        # 构造 Line 3 (底部青色状态栏，1:1 对标 2.4G | 档位 | 电池% | 回报率 样式)
        batt_tag = f"🔋 {k98.percent}%" if (k98 and k98.percent > 0) else "🔋 监控中"
        l3 = f"2.4G 模式  |  {batt_tag}  |  实时监控"

        with self._lock:
            self._line1 = l1
            self._line2 = l2
            self._line3 = l3

        dpi = _get_dpi_for_window(self.hwnd)
        S = lambda v: _scale_dpi(v, dpi)
        w = S(310)
        h = S(94)

        rc_work = RECT()
        user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rc_work), 0)
        x = rc_work.right - w - S(18)
        y = rc_work.bottom - h - S(18)

        user32.SetWindowPos(self.hwnd, -1, x, y, w, h, 0x0010 | 0x0040)
        user32.InvalidateRect(self.hwnd, None, True)

        self._hide_deadline = time.time() + 3.5
        if not self._anim_thread or not self._anim_thread.is_alive():
            self._anim_thread = threading.Thread(target=self._run_animation, daemon=True)
            self._anim_thread.start()

    def _run_animation(self):
        target_alpha = 245
        cur_alpha = self._alpha
        while cur_alpha < target_alpha:
            cur_alpha = min(target_alpha, cur_alpha + 35)
            self._alpha = cur_alpha
            user32.SetLayeredWindowAttributes(self.hwnd, COLOR_KEY, cur_alpha, LWA_COLORKEY | LWA_ALPHA)
            time.sleep(0.016)

        while time.time() < self._hide_deadline:
            time.sleep(0.05)

        while cur_alpha > 0:
            cur_alpha = max(0, cur_alpha - 20)
            self._alpha = cur_alpha
            user32.SetLayeredWindowAttributes(self.hwnd, COLOR_KEY, cur_alpha, LWA_COLORKEY | LWA_ALPHA)
            time.sleep(0.016)

        user32.ShowWindow(self.hwnd, 0)

    def destroy(self):
        if self.hwnd:
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None


osd_manager = NativeOsdManager()
