"""
添加新外设向导（一键抓包识别）
流程：扫描 → 选择端点 → 一键被动抓包(20s，零报文自动试已知查询指令)
     → 申报当前电量 → 字节偏移自动推断 → 生成配置 → 热重载生效
"""
import os
import json
import threading

import wx

from core.sniffer import HidSniffer
from config import DEVICES_DIR

# ─── MCHOSE 浅色主题令牌（与 dashboard.html 保持一致）───
C_BG = wx.Colour(0xF2, 0xF3, 0xF5)   # 页面浅灰底
C_CARD = wx.Colour(0xFF, 0xFF, 0xFF)  # 白卡片
C_CARD_HOVER = wx.Colour(0xE5, 0xE9, 0xF0)
C_TEXT = wx.Colour(0x15, 0x15, 0x15)  # 主文字深色
C_MUTED = wx.Colour(0x8A, 0x91, 0x99)  # 次级文字
C_ACCENT = wx.Colour(0x00, 0x53, 0xE2)  # 主蓝
C_GREEN = wx.Colour(0x00, 0xB5, 0x78)  # 保存绿
C_BORDER = wx.Colour(0xD7, 0xDE, 0xE3)  # 边框


def _darkize(win):
    win.SetBackgroundColour(C_BG)
    win.SetForegroundColour(C_TEXT)


class DeviceWizardDialog(wx.Dialog):
    """一键添加新外设：扫描 → 抓包 → 推断 → 保存生效"""

    def __init__(self, parent, on_saved_callback=None):
        super().__init__(parent, title="添加新外设 · 一键抓包识别",
                         size=wx.Size(860, 640),
                         style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER)
        self.SetBackgroundColour(C_BG)
        self.on_saved_callback = on_saved_callback
        self.scanned_items = []
        self._capturing = False
        self._capture_stop = False
        self._capture_frames = []
        self._inferred = None
        self._init_ui()
        self.CenterOnParent()
        self.on_refresh(None)

    # ─────────────────────── UI 构建 ───────────────────────

    def _init_ui(self):
        root = wx.BoxSizer(wx.VERTICAL)

        tip = wx.StaticText(self, label="① 选择设备端点 → ② 一键抓包（请在这 20 秒内操作该外设）→ ③ 填当前电量 → ④ 保存即生效")
        tip.SetForegroundColour(C_MUTED)
        root.Add(tip, 0, wx.ALL, 10)

        # Step 1 设备列表
        self.list_ctrl = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        for i, (col, w) in enumerate([("厂商 / 设备", 210), ("VID", 70), ("PID", 70),
                                      ("UsagePage", 90), ("Usage", 70), ("接口", 50)]):
            self.list_ctrl.InsertColumn(i, col, width=w)
        self.list_ctrl.SetBackgroundColour(C_CARD)
        self.list_ctrl.SetForegroundColour(C_TEXT)
        if hasattr(self.list_ctrl, "SetHeaderBackgroundColour"):
            self.list_ctrl.SetHeaderBackgroundColour(C_BG)
        root.Add(self.list_ctrl, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        hbar = wx.BoxSizer(wx.HORIZONTAL)
        self.chk_vendor = wx.CheckBox(self, label="仅显示供应商通道 (推荐)")
        self.chk_vendor.SetValue(True)
        self.chk_vendor.SetForegroundColour(C_TEXT)
        self.chk_vendor.Bind(wx.EVT_CHECKBOX, lambda e: self.render_list())
        hbar.Add(self.chk_vendor, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        btn_refresh = wx.Button(self, label="重新扫描")
        btn_refresh.SetBackgroundColour(C_CARD)
        btn_refresh.SetForegroundColour(C_TEXT)
        if hasattr(wx, "ART_REFRESH"):
            try:
                btn_refresh.SetBitmapLabel(
                    wx.ArtProvider.GetBitmap(wx.ART_REFRESH, wx.ART_BUTTON, (16, 16)))
            except Exception:
                pass
        btn_refresh.Bind(wx.EVT_BUTTON, self.on_refresh)
        hbar.Add(btn_refresh, 0)
        hbar.AddStretchSpacer()
        self.lbl_state = wx.StaticText(self, label="等待选择设备…")
        self.lbl_state.SetForegroundColour(C_ACCENT)
        hbar.Add(self.lbl_state, 0, wx.ALIGN_CENTER_VERTICAL)
        root.Add(hbar, 0, wx.EXPAND | wx.ALL, 10)

        # Step 2 抓包区
        card = wx.Panel(self)
        card.SetBackgroundColour(C_CARD)
        cs = wx.BoxSizer(wx.VERTICAL)

        h_cap = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_capture = wx.Button(card, label="一键抓包识别 (20 秒)")
        self.btn_capture.SetBackgroundColour(C_ACCENT)
        self.btn_capture.SetForegroundColour(wx.WHITE)
        self.btn_capture.Bind(wx.EVT_BUTTON, self.on_capture)
        h_cap.Add(self.btn_capture, 0, wx.RIGHT, 10)
        self.gauge = wx.Gauge(card, range=100, size=(-1, 14))
        h_cap.Add(self.gauge, 1, wx.ALIGN_CENTER_VERTICAL)
        cs.Add(h_cap, 0, wx.EXPAND | wx.ALL, 8)

        self.txt_output = wx.TextCtrl(card, style=wx.TE_MULTILINE | wx.TE_READONLY, size=(-1, 110))
        self.txt_output.SetBackgroundColour(C_CARD)
        self.txt_output.SetForegroundColour(C_ACCENT)
        cs.Add(self.txt_output, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        root.Add(card, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        # Step 3/4 申报电量与保存
        h_bot = wx.BoxSizer(wx.HORIZONTAL)
        h_bot.Add(wx.StaticText(self, label="设备当前电量 %:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.spin_pct = wx.SpinCtrl(self, min=0, max=100, initial=50)
        self.spin_pct.Bind(wx.EVT_SPINCTRL, lambda e: self.do_infer())
        h_bot.Add(self.spin_pct, 0, wx.RIGHT, 14)
        h_bot.Add(wx.StaticText(self, label="设备别名:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.txt_name = wx.TextCtrl(self, value="我的新外设")
        h_bot.Add(self.txt_name, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 14)
        h_bot.Add(wx.StaticText(self, label="类型:"), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.choice_type = wx.Choice(self, choices=["鼠标", "键盘"])
        self.choice_type.SetSelection(0)
        h_bot.Add(self.choice_type, 0, wx.RIGHT, 14)
        self.btn_save = wx.Button(self, label="保存并加入监控")
        self.btn_save.SetBackgroundColour(C_GREEN)
        self.btn_save.SetForegroundColour(wx.WHITE)
        self.btn_save.Bind(wx.EVT_BUTTON, self.on_save)
        h_bot.Add(self.btn_save, 0)
        root.Add(h_bot, 0, wx.EXPAND | wx.ALL, 10)

        self.SetSizer(root)
        self.list_ctrl.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_item_selected)

    # ─────────────────────── 扫描与列表 ───────────────────────

    def on_refresh(self, event):
        self.scanned_items = HidSniffer.scan_all_devices()
        self.render_list()

    def render_list(self):
        self.list_ctrl.DeleteAllItems()
        vendor_only = self.chk_vendor.GetValue()
        for d in self.scanned_items:
            if vendor_only and not d["is_vendor"]:
                continue
            idx = self.list_ctrl.InsertItem(self.list_ctrl.GetItemCount(),
                                            f"{d['manufacturer']} {d['product']}".strip() or "未知设备")
            self.list_ctrl.SetItem(idx, 1, d["vendor_id"])
            self.list_ctrl.SetItem(idx, 2, d["product_id"])
            self.list_ctrl.SetItem(idx, 3, d["usage_page"])
            self.list_ctrl.SetItem(idx, 4, d["usage"])
            self.list_ctrl.SetItem(idx, 5, str(d["interface"]))
            self.list_ctrl.SetItemData(idx, self.scanned_items.index(d))

    def _selected(self):
        idx = self.list_ctrl.GetFirstSelected()
        if idx >= 0:
            return self.scanned_items[self.list_ctrl.GetItemData(idx)]
        return None

    def on_item_selected(self, event):
        dev = self._selected()
        if dev:
            self.txt_name.SetValue(f"{dev['manufacturer']} {dev['product']}".strip() or "新外设")
            self.lbl_state.SetLabel(f"已选 {dev['vendor_id']}:{dev['product_id']} (UP:{dev['usage_page']})")

    # ─────────────────────── 一键抓包 ───────────────────────

    def on_capture(self, event):
        dev = self._selected()
        if not dev:
            self._notice("⚠ 请先在列表中选择设备端点")
            return
        if self._capturing:
            self._capture_stop = True
            return

        self._capturing = True
        self._capture_stop = False
        self._capture_frames = []
        self.btn_capture.SetLabel("停止并分析")
        self.txt_output.SetValue("抓包中… 请持续操作该外设（移动/打字）\n")
        raw_path = dev["raw_path"]

        def progress(elapsed, frames):
            wx.CallAfter(self._on_progress, elapsed, frames)

        def worker():
            res = HidSniffer.capture_stream(raw_path, duration_s=20.0,
                                            should_stop=lambda: self._capture_stop,
                                            progress_cb=progress)
            frames = res.get("frames", [])
            if not frames and not res.get("cancelled"):
                wx.CallAfter(self._log, "\n被动监听零报文，自动尝试已知查询指令…\n")
                q = HidSniffer.query_probe(raw_path)
                frames = q.get("frames", [])
                for t in q.get("tried", []):
                    wx.CallAfter(self._log, f"  [{t['name']}] 写入{t['written']}B → 响应{t['got']}B\n")
            wx.CallAfter(self._on_capture_done, frames)

        threading.Thread(target=worker, daemon=True).start()

    def _on_progress(self, elapsed, frames):
        self.gauge.SetValue(min(100, int(elapsed / 20.0 * 100)))
        self.lbl_state.SetLabel(f"捕获中 {elapsed:.0f}s / 报文 {frames} 帧")

    def _log(self, text):
        self.txt_output.AppendText(text)

    def _on_capture_done(self, frames):
        self._capturing = False
        self._capture_frames = frames
        self.btn_capture.SetLabel("一键抓包识别 (20 秒)")
        self.gauge.SetValue(100 if frames else 0)

        if not frames:
            self._log("\n❌ 未捕获任何报文。请确认：① 已退出该设备官方驱动 ② 设备为 2.4G/USB 连接 ③ 抓包期间有操作设备\n")
            self.lbl_state.SetLabel("抓包失败，请重试")
            return

        self._log(f"\n✅ 共捕获 {len(frames)} 帧，示例：\n")
        for f in frames[:4]:
            self._log("  " + " ".join(f"0x{b:02X}" for b in f["raw"][:16]) + " …\n")
        self.do_infer()

    # ─────────────────────── 偏移推断 ───────────────────────

    def do_infer(self):
        if not self._capture_frames:
            return
        pct = self.spin_pct.GetValue()
        r = HidSniffer.infer_battery_offset(self._capture_frames, pct)
        self._inferred = r.get("best")
        if self._inferred:
            off = self._inferred["offset"]
            self._log(f"\n🎯 推断命中：byte[{off}] 与申报电量 {pct}% 匹配 "
                      f"(命中{self._inferred['hits']}次) —— 可直接保存\n")
            self.lbl_state.SetLabel(f"已识别电量字节 byte[{off}]")
        else:
            self._log(f"\n⚠️ 未找到与 {pct}% 匹配的字节。请确认申报电量后重试，或捕获更久。\n")
            self.lbl_state.SetLabel("未匹配，请核对电量数值")

    # ─────────────────────── 保存生效 ───────────────────────

    def _notice(self, text: str):
        """内嵌提示：写入日志区 + 状态标签，不弹系统对话框（约束第 11 条）"""
        try:
            self._log(f"\n{text}\n")
            self.lbl_state.SetLabel(text.splitlines()[0][:60])
        except Exception:
            print(f"[Wizard] {text}")

    def on_save(self, event):
        dev = self._selected()
        if not dev:
            self._notice("⚠ 请先在列表中选择设备端点")
            return
        if not self._inferred:
            self._notice("⚠ 请先完成一键抓包并成功识别电量字节")
            return

        file_id = f"device_{dev['vendor_id'][2:].lower()}_{dev['product_id'][2:].lower()}"
        cfg = {
            "id": file_id,
            "name": self.txt_name.GetValue().strip() or "自定义外设",
            "device_type": "keyboard" if self.choice_type.GetSelection() == 1 else "mouse",
            "vendor_id": dev["vendor_id"],
            "product_ids": [dev["product_id"]],
            "usage_page": dev["usage_page"],
            "usage": dev["usage"],
            "protocol": "standard_hid",
            "enabled": True,
            "description": f"一键向导自动生成 ({dev['manufacturer']} {dev['product']})",
            "protocol_custom": {
                "query_hex": [],
                "battery_byte_offset": self._inferred["offset"],
                "charging_byte_offset": -1,
                "charging_bit_mask": 0,
                "xor_mask": 0
            }
        }

        target = os.path.join(DEVICES_DIR, f"{file_id}.json")
        try:
            with open(target, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self._notice(f"❌ 保存失败：{e}")
            return

        if self.on_saved_callback:
            try:
                self.on_saved_callback()
            except Exception as e:
                print(f"[Wizard] 热重载回调异常: {e}")

        self._notice(f"✅ 已保存 {os.path.basename(target)}，设备已加入实时监控")
        # 给用户 1.2 秒看清结果再关窗
        wx.CallLater(1200, self.Close)
