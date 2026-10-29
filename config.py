import os
import sys
import json
import winreg
import ctypes

# PyInstaller 打包后：sys.frozen=True 表示在 exe 运行
# - BASE_DIR 用于数据持久化（数据库、设置、devices）→ exe 所在目录
# - MEIPASS 用于只读资源文件（HTML、png）→ PyInstaller 临时解压目录
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
    RESOURCE_DIR = sys._MEIPASS
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    RESOURCE_DIR = BASE_DIR

DATA_DIR = os.path.join(BASE_DIR, "data")
DEVICES_DIR = os.path.join(BASE_DIR, "devices")

# 诊断日志：帮助排查打包后路径问题
if getattr(sys, 'frozen', False):
    print(f"[Config] frozen=True")
    print(f"[Config] BASE_DIR={BASE_DIR}")
    print(f"[Config] RESOURCE_DIR={RESOURCE_DIR}")
    print(f"[Config] DEVICES_DIR={DEVICES_DIR}, exists={os.path.isdir(DEVICES_DIR)}")
    _meipass_devices = os.path.join(RESOURCE_DIR, "devices")
    print(f"[Config] _meipass_devices={_meipass_devices}, exists={os.path.isdir(_meipass_devices)}")
    if os.path.isdir(RESOURCE_DIR):
        try:
            _contents = os.listdir(RESOURCE_DIR)
            print(f"[Config] RESOURCE_DIR contents: {_contents[:30]}")
        except Exception as _e:
            print(f"[Config] RESOURCE_DIR listdir 失败: {_e}")

SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
DB_FILE = os.path.join(DATA_DIR, "battery_stats.db")

# Windows Run Registry Key for Auto-Start
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "Volt"

# WebView2 缓存路径：采用 Windows 8.3 短路径，彻底杜绝中文路径导致的 0x80004004 网页初始化失败
def _get_short_path(path: str) -> str:
    try:
        buf = ctypes.create_unicode_buffer(500)
        ctypes.windll.kernel32.GetShortPathNameW(path, buf, 500)
        return buf.value or path
    except Exception:
        return path

_local_appdata = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
_app_storage = os.path.join(_local_appdata, "Volt")
os.makedirs(_app_storage, exist_ok=True)
WEBVIEW2_CACHE_DIR = os.path.join(_get_short_path(_app_storage), "WebView2")
os.makedirs(WEBVIEW2_CACHE_DIR, exist_ok=True)
os.environ["WEBVIEW2_USER_DATA_FOLDER"] = WEBVIEW2_CACHE_DIR
# GPU 兜底：Intel iGPU + 高 DPI 下 WebView2 硬件加速可能黑屏，软件渲染对仪表盘无损
os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "--disable-gpu")

DEFAULT_SETTINGS = {
    # 轮询间隔不在这里配 —— 每台设备的 devices/*.json 里各有 poll_interval，
    # 不同外设的应答速度差别很大，统一配置只会帮倒忙。
    "warning_threshold": 20,    # 黄色预警阈值 (%) ── 已被托盘与 OSD 实际读取
    "critical_threshold": 10,   # 红色告警阈值 (%) ── 同上
    "auto_start": False,        # 开机自启动
    "enable_logging": True      # 是否把运行日志写入 data/volt.log
}

class ConfigManager:
    def __init__(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(DEVICES_DIR, exist_ok=True)
        self.settings = dict(DEFAULT_SETTINGS)
        self.load()

    def load(self):
        if os.path.exists(SETTINGS_FILE):
            try:
                with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.settings.update(data)
            except Exception as e:
                print(f"[Config] Error loading settings: {e}")
        else:
            self.save()

    def save(self):
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[Config] Error saving settings: {e}")

    def get(self, key, default=None):
        return self.settings.get(key, default)

    def set(self, key, value):
        self.settings[key] = value
        self.save()
        if key == "auto_start":
            self.apply_auto_start(value)

    def is_auto_start_enabled(self) -> bool:
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ)
            val, _ = winreg.QueryValueEx(key, APP_NAME)
            winreg.CloseKey(key)
            return bool(val)
        except OSError:
            return False

    def _launch_command(self) -> str:
        """自启动要写进注册表的命令行"""
        if getattr(sys, 'frozen', False):
            return sys.executable
        # 开发模式用 pythonw 拉脚本，避免开机弹黑框
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.exists(pythonw):
            pythonw = sys.executable
        script = os.path.join(BASE_DIR, "main.py")
        return f'"{pythonw}" "{script}"'

    def apply_auto_start(self, enable: bool):
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE)
            if enable:
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, self._launch_command())
            else:
                try:
                    winreg.DeleteValue(key, APP_NAME)
                except OSError:
                    pass
            winreg.CloseKey(key)
        except Exception as e:
            print(f"[Config] Failed to set auto-start: {e}")

    def verify_auto_start(self):
        """启动时校验自启动项是否还指向当前程序。

        用户把 exe 从 dist/ 挪到别处、或者改名后，注册表里还是旧路径，
        开机时会被系统静默跳过 —— 用户以为"自启动坏了"。这里自愈。
        """
        if not self.get("auto_start", False):
            return
        cur = self._launch_command()
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ)
            try:
                val, _ = winreg.QueryValueEx(key, APP_NAME)
            finally:
                winreg.CloseKey(key)
            if val != cur:
                print(f"[Config] 自启动路径已失效（注册表={val}），自动修复为 {cur}")
                self.apply_auto_start(True)
        except OSError:
            print("[Config] 自启动项丢失，自动补回")
            self.apply_auto_start(True)

config = ConfigManager()
