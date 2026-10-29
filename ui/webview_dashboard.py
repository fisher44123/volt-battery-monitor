"""
迈从 MCHOSE HUB 风格外设控制中心主窗口
基于 Edge WebView2 渲染的高性能、极速响应的前端界面，深度绑定 Python 底层外设引擎与续航统计系统。
- 窗口行为：右上角“X”直接退出程序；最小化“-”收起到系统托盘。
- 实时数据：两台外设实时电量、智能结合续航小时/天数预估、放电速率、平滑趋势图。
"""
import json
import os
import time
from typing import Dict, Optional

from config import config, RESOURCE_DIR, DATA_DIR

import wx
import wx.html2
from core.engine import DeviceEngine
from core.stats import stats_manager

UI_DIR = os.path.join(RESOURCE_DIR, "ui")
HTML_PATH = os.path.join(UI_DIR, "assets", "dashboard.html")

# HTML 资源存在性预检：缺失时立即日志化（黑屏第二大根因）
if not os.path.exists(HTML_PATH):
    print(f"[Volt][FATAL] dashboard.html 不存在: {HTML_PATH}")
    print(f"[Volt][FATAL] RESOURCE_DIR={RESOURCE_DIR} 内容: {os.listdir(RESOURCE_DIR) if os.path.isdir(RESOURCE_DIR) else '目录不存在'}")


import ctypes


class WebViewDashboardFrame(wx.Frame):
    def __init__(self, parent, engine: DeviceEngine):
        super().__init__(
            parent,
            title="Volt 外设控制中心",
            size=(1000, 680),
            # 原生标题栏回归：X 100% 在右上角且点击有效，DWM 深色化融入主题
            style=wx.DEFAULT_FRAME_STYLE,
        )
        try:
            self.SetSize(self.FromDIP(wx.Size(1000, 680)))
            self.SetMinSize(self.FromDIP(wx.Size(880, 580)))
        except Exception:
            pass

        self.engine = engine
        self._web_ready = False
        self._last_push = 0.0
        # 推送去重：设备 -> 内容签名；以及每 5 秒一次的强制补推
        self._last_sig: Dict[str, tuple] = {}
        self._last_force_push = 0.0

        self.SetBackgroundColour(wx.Colour(0xF2, 0xF3, 0xF5))
        ico = os.path.join(RESOURCE_DIR, "assets", "app_icon.ico")
        if os.path.exists(ico):
            self.SetIcon(wx.Icon(ico))

        # 浅色主题：标题栏跟随系统浅色（不再 DWM 深色化，与浅色内容一致）

        try:
            self.webview = wx.html2.WebView.New(self, backend=wx.html2.WebViewBackendEdge)
        except Exception as e:
            # WebView2 创建失败是黑屏的最常见根因，必须落盘可查
            print(f"[Volt][FATAL] WebView2(Edge) 创建失败: {e}")
            print(f"[Volt][FATAL] WEBVIEW2_USER_DATA_FOLDER={os.environ.get('WEBVIEW2_USER_DATA_FOLDER')}")
            import traceback
            traceback.print_exc()
            wx.MessageBox(
                f"WebView2 内核初始化失败：{e}\n\n"
                "请安装 Microsoft Edge WebView2 Runtime 后重试。\n"
                f"详细日志: {os.path.join(DATA_DIR, 'volt.log')}",
                "Volt 启动失败", wx.OK | wx.ICON_ERROR)
            raise
        # 注意：handler 名不能是 "chrome" —— wxWebView2 会用该名覆盖原生 window.chrome，
        # 导致 window.chrome.webview 变 undefined，JS→Python 消息全部静默丢失。
        # 改用 "volt"，dashboard.html 里 window.volt.postMessage 与之对应。
        self.webview.AddScriptMessageHandler("volt")
        self.webview.Bind(wx.html2.EVT_WEBVIEW_SCRIPT_MESSAGE_RECEIVED, self._on_js_message)
        self.webview.Bind(wx.html2.EVT_WEBVIEW_LOADED, self._on_page_loaded)
        self.webview.Bind(wx.html2.EVT_WEBVIEW_NAVIGATING, self._on_navigating)

        sizer = wx.BoxSizer(wx.VERTICAL)
        sizer.Add(self.webview, 1, wx.EXPAND)
        self.SetSizer(sizer)
        self.Center()

        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.Bind(wx.EVT_ICONIZE, self._on_iconize)

        # 400ms 定时对齐数据状态
        self._timer = wx.Timer(self)
        self._timer.Bind(wx.EVT_TIMER, lambda e: self.push_all_statuses())
        self._timer.Start(400)

        # pathlib.as_uri() 对中文路径做正确的百分号编码，附带动态时间戳彻底绕过 WebView2 磁盘缓存
        from pathlib import Path
        url = f"{Path(HTML_PATH).as_uri()}?_v={int(time.time())}"
        print(f"[Volt] WebView 加载: {url}")
        self.webview.LoadURL(url)

    def _on_iconize(self, event):
        """窗口最小化：收起到系统托盘"""
        if event.IsIconized():
            self.Hide()

    def _on_close(self, event):
        """用户点击关闭：按用户决策彻底退出程序"""
        self._timer.Stop()
        self.Destroy()
        app = wx.GetApp()
        anchor = getattr(app, "_anchor", None)
        if anchor:
            try:
                anchor.Destroy()
            except Exception:
                pass
        app.ExitMainLoop()

    def _on_page_loaded(self, event):
        self._web_ready = True
        print("[Volt] 页面加载完成（EVT_WEBVIEW_LOADED）")
        wx.CallLater(100, self.push_all_statuses)

    def _on_navigating(self, event):
        url = event.GetURL()
        if url and not url.startswith("file://") and url != "about:blank":
            event.Veto()

    def on_opened(self):
        self.push_all_statuses()

    # ─────────────── 前端消息分发 ───────────────

    def _on_js_message(self, event):
        try:
            msg = json.loads(event.GetString())
        except Exception:
            print(f"[Volt][JS] 无法解析消息: {event.GetString()[:300]}")
            return
        act = msg.get("action")
        if act == "dom_ready":
            # 前端页面加载完成：JS 桥接已通，立即推送一次状态。
            # 这行日志是排查「黑屏 / 数据不刷新」的第一现场证据。
            self._web_ready = True
            print("[Volt] 前端已就绪（JS 桥接握手成功）")
            self.push_all_statuses()
            return
        if act == "js_error":
            # 前端 JS 异常上报 —— 黑屏排查关键链路
            print(f"[Volt][JS ERROR] {msg.get('message')} @ {msg.get('source')}:{msg.get('line')}")
            return
        if act == "minimize":
            self.Hide()
        elif act == "close":
            self.Close()
        elif act == "maximize":
            if self.IsMaximized():
                self.Restore()
            else:
                self.Maximize()
        elif act == "refresh":
            self.engine.poll_once()
            self.push_all_statuses()
        elif act == "export_csv":
            dev_id = msg.get("device_id", "lingbao_k98")
            self._do_export_csv(dev_id)
        elif act == "set_auto_start":
            enabled = bool(msg.get("enabled", False))
            config.set("auto_start", enabled)
        elif act == "add_device":
            # 一键添加外设向导（保存后热重载引擎）；异常必须落日志
            try:
                from ui.device_wizard import DeviceWizardDialog
                dlg = DeviceWizardDialog(self, on_saved_callback=self.engine.reload_devices)
                dlg.ShowModal()
                dlg.Destroy()
                self.engine.poll_once()
                self.push_all_statuses()
            except Exception as e:
                import traceback
                print(f"[Volt][FATAL] 打开添加外设向导失败: {e}")
                traceback.print_exc()
        else:
            print(f"[Volt][JS] 未知消息: {msg}")

    def _do_export_csv(self, dev_id: str):
        """导出到桌面。用前端内嵌提示代替弹窗（约束：禁止弹窗式提示）。"""
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        target_csv = os.path.join(desktop, f"{dev_id}_历史电量续航数据.csv")
        if stats_manager.export_csv(dev_id, target_csv):
            self.show_toast(f"已导出到桌面：{os.path.basename(target_csv)}", "ok")
        else:
            self.show_toast("暂无记录可导出，用一段时间后再试", "warn")

    # ─────────────── 向前端注入数据 ───────────────

    def push_all_statuses(self):
        """把引擎状态推给前端。

        性能要点（旧实现的坑）：
        - 旧版每 400ms、每设备要跑 5 次 SQLite 查询（其中耗电速率被算了两遍），
          还每次新建 10 个数据库连接。现在合并成一次 get_ui_snapshot，全部走缓存。
        - 旧版每次无条件 json.dumps（含 168 小时历史）后 RunScript 注入，
          哪怕数据一个字都没变。现在按签名去重，只在真变化时推，另外每 5 秒
          强制补推一次以保证「同步时间 / X 分钟前」这类相对时间能走字。
        """
        if not self._web_ready:
            return
        statuses = self.engine.get_all_statuses()
        now = time.time()

        force = (now - self._last_force_push) >= 5.0
        if force:
            self._last_force_push = now

        for dev_id, st in statuses.items():
            pct = st.percent if st.percent > 0 else 0
            raw_info = getattr(st, "raw_info", "") or ""

            # 一次取齐：放电速率 / 剩余时长 / 上次充满 / 循环次数 / 168h 历史
            snap = stats_manager.get_ui_snapshot(dev_id, pct)
            history_pts = snap["history"]

            # 真实硬件同步时间（证明数据秒级实时、非虚假模拟）
            if st.timestamp > 0:
                elapsed = now - st.timestamp
                if elapsed < 60:
                    sync_time_str = f"刚刚 ({time.strftime('%H:%M', time.localtime(st.timestamp))})"
                elif elapsed < 3600:
                    sync_time_str = f"{int(elapsed // 60)} 分钟前"
                else:
                    sync_time_str = time.strftime('%H:%M:%S', time.localtime(st.timestamp))
            else:
                sync_time_str = "刚刚"

            if raw_info == "Sleeping":
                sync_time_str += " · 💤休眠待命"

            rem_text = snap["remText"]
            if raw_info == "Sleeping" and pct > 0:
                rem_text = "外设已休眠待命"

            payload = {
                "percent": st.percent,
                "charging": st.charging,
                "online": st.online,
                # 算不出速率时给 None，前端显示 "--"。旧实现在这里硬编码
                # 0.8 / 1.2 冒充真实放电速率，属于虚构数值。
                "rate": snap["rate"],
                "remText": rem_text,
                "syncTime": sync_time_str,
                "lastFull": snap["lastFull"],
                "cycleText": snap["cycleText"],
                "history": history_pts,
                "autoStart": bool(config.get("auto_start", False)),
                "raw_info": raw_info,
            }

            # 签名只包含"内容"，不含相对时间文案
            sig = (st.percent, st.charging, st.online, raw_info,
                   snap["rate"], snap["lastFull"], snap["cycleText"],
                   len(history_pts))
            if not force and self._last_sig.get(dev_id) == sig:
                continue
            self._last_sig[dev_id] = sig

            js_code = (f"if(window.updateDeviceStatus) {{ window.updateDeviceStatus("
                       f"'{dev_id}', {json.dumps(payload, ensure_ascii=False)}); }}")
            try:
                self.webview.RunScript(js_code)
            except Exception as e:
                print(f"[Volt] 注入 {dev_id} 状态失败: {e}")

    def show_toast(self, message: str, kind: str = "ok"):
        """前端内嵌轻提示（替代 wx.MessageBox 弹窗，遵守「不弹窗」约束）"""
        if not self._web_ready:
            return
        try:
            safe = json.dumps(message, ensure_ascii=False)
            self.webview.RunScript(
                f"if(window.showToast){{window.showToast({safe}, '{kind}');}}")
        except Exception:
            pass
