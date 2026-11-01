"""
README 截图生成器：一次截取主界面 + 托盘卡片两张图。

用法（先用 C:\Python313\python.exe，装齐依赖后）：
    python scripts/take_screenshots.py

输出到 docs/screenshots/，README 直接引用。
截图内容是真实硬件读数，运行前确认设备已插好。
"""
import ctypes
import os
import sys
import time

ctypes.windll.user32.SetProcessDPIAware()

import wx
from PIL import ImageGrab

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)
os.chdir(PROJ)

from core.engine import DeviceEngine            # noqa: E402
from ui.tray_card import TrayCardPopup          # noqa: E402
from ui.webview_dashboard import WebViewDashboardFrame  # noqa: E402

OUT_DIR = os.path.join(PROJ, "docs", "screenshots")


def grab_window(win, path):
    """用 PrintWindow 直接从窗口渲染层抓图。

    为什么不用 ImageGrab.grab(bbox)：
    1. SetForegroundWindow 受 Windows 前台锁定策略限制，后台进程抢不到焦点，
       目标窗口被其它窗口遮挡时会截到别人的画面（实测截到过浏览器，隐私事故）；
    2. PW_RENDERFULLCONTENT 能抓到 WebView2 这类 GPU 合成层的内容，
       且窗口被遮挡也照常抓取，完全不打扰当前正在用的窗口。
    """
    import ctypes
    import ctypes.wintypes as wt
    from PIL import Image

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    user32.SetProcessDPIAware()

    hwnd = win.GetHandle()
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top

    hdc_win = user32.GetWindowDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
    bmp = gdi32.CreateCompatibleBitmap(hdc_win, w, h)
    gdi32.SelectObject(hdc_mem, bmp)

    PW_RENDERFULLCONTENT = 2
    ok = user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT)

    class BMIH(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG),
                    ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                    ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                    ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                    ("biClrImportant", wt.DWORD)]

    bmi = BMIH()
    bmi.biSize = ctypes.sizeof(BMIH)
    bmi.biWidth = w
    bmi.biHeight = -h          # 自上而下
    bmi.biPlanes = 1
    bmi.biBitCount = 32

    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(hdc_mem, bmp, 0, h, buf, ctypes.byref(bmi), 0)

    img = Image.frombuffer("RGBA", (w, h), buf.raw, "raw", "BGRA", 0, 1)
    img = img.convert("RGB")

    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(hwnd, hdc_win)

    if not ok:
        print(f"  警告: PrintWindow 返回失败，{os.path.basename(path)} 可能不完整")
    img.save(path)
    print(f"  已保存 {os.path.basename(path)}  {img.size}")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    app = wx.App(False)
    app.SetExitOnFrameDelete(False)
    anchor = wx.Frame(None, size=(1, 1), style=wx.NO_BORDER)
    anchor.Hide()

    engine = DeviceEngine()
    engine.poll_once()

    state = {}

    dash = WebViewDashboardFrame(None, engine)
    dash.Show()
    state["dash"] = dash

    def shot_card():
        card = TrayCardPopup(engine, {"dashboard": lambda: None, "exit": lambda: None})
        card.SetPosition((200, 200))
        card.Show()
        card.Raise()
        state["card"] = card
        wx.CallLater(800, shot_all)

    def shot_all():
        # Hero 默认显示第一台设备；若它恰好休眠/离线，切到有读数的那台，
        # 保证 README 截图展示的是"正常工作"状态
        try:
            for dev_id in ("mchose_g3v2", "lingbao_k98"):
                st = engine.get_status(dev_id)
                if st and st.online and st.percent > 0:
                    dash.webview.RunScript(f"if(window.switchDevice) switchDevice('{dev_id}')")
                    wx.MilliSleep(700)
                    break
        except Exception as e:
            print("  切换设备失败(不影响截图):", e)

        grab_window(state["dash"], os.path.join(OUT_DIR, "dashboard.png"))
        if state.get("card"):
            grab_window(state["card"], os.path.join(OUT_DIR, "tray-card.png"))
        print("完成")
        app.ExitMainLoop()

    # 等前端就绪 + 两台设备完成首次真实读数（键盘 30 秒轮询，首帧来自 on_open 即时查询）；
    # WebView2 首次初始化较慢，留足余量，截早了会得到全黑帧
    wx.CallLater(14000, shot_card)
    app.MainLoop()


if __name__ == "__main__":
    main()
