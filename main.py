import sys
import os
import time
import ctypes
from typing import Optional
from ctypes import wintypes

# 控制台中文输出统一为 UTF-8，避免 GBK 代码页下日志出现乱码
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from config import config, APP_NAME, BASE_DIR, DATA_DIR


def setup_logging():
    """把 stdout/stderr 落盘到 data/volt.log —— --noconsole 打包模式下唯一可观测手段。

    旧实现用 open(..., "w") 每次启动清空日志，且每行都没有时间戳，
    出问题根本没法回溯「几点几分发生了什么」。现在改为：
    - 追加写入 + 超过 2MB 自动滚成 volt.log.1（保留一份上一轮的历史）
    - 每行前置 `YYYY-MM-DD HH:MM:SS | ` 时间戳
    - 尊重 settings.json 里的 enable_logging 开关
    """
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        if not config.get("enable_logging", True):
            return

        log_path = os.path.join(DATA_DIR, "volt.log")
        try:
            if os.path.exists(log_path) and os.path.getsize(log_path) > 2 * 1024 * 1024:
                old = log_path + ".1"
                if os.path.exists(old):
                    os.remove(old)
                os.replace(log_path, old)
        except Exception:
            pass

        log_file = open(log_path, "a", encoding="utf-8", buffering=1)

        class _TimestampedStream:
            """给每行补时间戳的流包装（print 会分多次 write，需按行缓冲）"""

            def __init__(self, fh):
                self._f = fh
                self._buf = ""

            def write(self, s):
                if not s:
                    return 0
                self._buf += s
                while "\n" in self._buf:
                    line, self._buf = self._buf.split("\n", 1)
                    ts = time.strftime("%Y-%m-%d %H:%M:%S")
                    self._f.write(f"{ts} | {line}\n")
                return len(s)

            def flush(self):
                try:
                    self._f.flush()
                except Exception:
                    pass

            def isatty(self):
                return False

        stream = _TimestampedStream(log_file)
        sys.stdout = stream
        sys.stderr = stream

        import traceback

        def _excepthook(t, v, tb):
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            log_file.write(f"\n{ts} | [FATAL] {t.__name__}: {v}\n")
            traceback.print_exception(t, v, tb, file=log_file)
            log_file.flush()

        sys.excepthook = _excepthook
        print(f"[Volt] ===== 启动 {log_path} =====")
        print(f"[Volt] BASE_DIR={BASE_DIR} frozen={getattr(sys, 'frozen', False)}")
    except Exception:
        pass


setup_logging()

import wx
from core.engine import DeviceEngine
from ui.tray import TrayManager

def enable_dpi_awareness():
    """启用 Windows 高分屏抗模糊缩放支持 (Per-Monitor V2)"""
    try:
        user32 = ctypes.windll.user32
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass

EVENT_NAME = f"Local\\{APP_NAME}_ShowWindow_Event"

def check_single_instance() -> Optional[wintypes.HANDLE]:
    """单实例互斥锁；若已有实例运行，唤醒它的主窗口而非静默退出"""
    kernel32 = ctypes.windll.kernel32
    mutex_name = f"Local\\{APP_NAME}_SingleInstance_Mutex"

    # 先创建命名事件供唤醒用（首个实例创建，后续实例打开）
    EVENT_MODIFY_STATE = 0x0002
    h_event = kernel32.CreateEventW(None, False, False, EVENT_NAME)

    h_mutex = kernel32.CreateMutexW(None, True, mutex_name)
    last_error = kernel32.GetLastError()
    if last_error == 183: # ERROR_ALREADY_EXISTS
        if h_mutex:
            kernel32.CloseHandle(h_mutex)
        # 唤醒已有实例：设置事件 → 旧实例主线程收到后弹出主窗口
        if h_event:
            kernel32.SetEvent(h_event)
            kernel32.CloseHandle(h_event)
        return None
    return (h_mutex, h_event)


def watch_wake_event(h_event, callback):
    """后台线程监听唤醒事件：第二个实例启动时弹出主窗口"""
    import threading
    kernel32 = ctypes.windll.kernel32
    def _watch():
        WAIT_OBJECT_0 = 0
        while True:
            ret = kernel32.WaitForSingleObject(h_event, 0xFFFFFFFF)  # INFINITE
            if ret == WAIT_OBJECT_0:
                try:
                    import wx
                    wx.CallAfter(callback)
                except Exception:
                    pass
    t = threading.Thread(target=_watch, daemon=True)
    t.start()

class VoltApp(wx.App):
    def OnInit(self):
        self.SetExitOnFrameDelete(False)
        # wxPython 在没有顶层窗口时会让 MainLoop 立即退出。
        # 创建一个 1x1 的完全隐藏帧来锚定消息循环。
        # 这是 wx 纯托盘程序的标准做法（见 wx wiki / Phoenix demo）。
        self._anchor = wx.Frame(None, size=(1, 1), style=wx.NO_BORDER)
        self._anchor.Hide()
        return True

def main():
    enable_dpi_awareness()

    # 自启动自愈：exe 被挪位置/改名后，注册表里的旧路径会让开机启动静默失效
    config.verify_auto_start()

    inst = check_single_instance()
    if not inst:
        print("[Volt] 已经在运行中，已唤醒其主窗口。")
        sys.exit(0)
    h_mutex, h_event = inst

    app = VoltApp()

    tray_mgr = None

    def on_device_update(statuses):
        def do_update():
            if tray_mgr:
                tray_mgr.update_all()
        wx.CallAfter(do_update)

    # 1. 初始化外设引擎 (遵循持久化状态机制)
    engine = DeviceEngine(on_update_callback=on_device_update)

    # 2. 初始化托盘与迈从 MCHOSE HUB 风格控制中心
    from ui.webview_dashboard import WebViewDashboardFrame
    dashboard = WebViewDashboardFrame(None, engine)
    app._dashboard = dashboard
    dashboard.Show()

    # 第二实例启动时唤醒本实例的主窗口
    def _wake_show():
        try:
            if dashboard.IsIconized():
                dashboard.Iconize(False)
            dashboard.Show()
            dashboard.Raise()
        except Exception:
            pass
    watch_wake_event(h_event, _wake_show)

    tray_mgr = TrayManager(engine)

    # 3. 初始扫描并启动后台守护监听
    engine.poll_once()
    engine.start()

    print("[Volt] 外设控制中心已就绪。")

    try:
        app.MainLoop()
    finally:
        engine.stop()
        if h_mutex:
            ctypes.windll.kernel32.CloseHandle(h_mutex)
        if h_event:
            ctypes.windll.kernel32.CloseHandle(h_event)

if __name__ == "__main__":
    main()
