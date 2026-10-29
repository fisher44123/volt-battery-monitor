import io
import math
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import wx

def _draw_lightning_bolt(draw: ImageDraw.ImageDraw, cx: float, cy: float, size: float, color=(255, 255, 255, 255)):
    """绘制高精度抗锯齿矢量闪电徽标（解决 Emoji 在不同 Windows 版本下回退乱码/方框的问题）"""
    w = size * 0.7
    h = size
    # 闪电折角 6 个关键顶点
    pts = [
        (cx - w * 0.15, cy - h * 0.50), # 顶点
        (cx + w * 0.40, cy - h * 0.50), # 顶宽
        (cx - w * 0.05, cy - h * 0.05), # 中内拐
        (cx + w * 0.50, cy - h * 0.05), # 中突刺
        (cx - w * 0.45, cy + h * 0.50), # 底尖
        (cx - w * 0.10, cy + h * 0.05), # 底内拐
        (cx - w * 0.45, cy + h * 0.05), # 左折角
    ]
    draw.polygon(pts, fill=color)


def create_battery_icon_image(
    percent: int,
    charging: bool = False,
    online: bool = True,
    device_type: str = "mouse",
    is_dark: bool = True,
    size: int = 32
) -> Image.Image:
    """
    使用 4x 超采样 + 矢量几何 + 任务栏自适应双层描边生成高品质系统托盘电池图标。
    支持 Windows 11/10 浅色与深色任务栏高对比度清晰呈现。
    """
    scale = 4
    W = size * scale
    H = size * scale

    # 创建高分辨率 RGBA 画布
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 颜色配置（鲜活现代桌面科技色标）
    # 外框：采用微半透浅白 + 1px 深色微阴影，确保在亮色和暗色任务栏下均清晰可见
    c_frame = (255, 255, 255, 245)
    c_shadow = (0, 0, 0, 110)

    if not online or percent < 0:
        c_fill = (140, 148, 160, 200) # 离线冷静灰
    elif charging:
        c_fill = (0, 122, 255, 255)   # 充电灵动蓝
    elif percent > 20:
        c_fill = (16, 185, 129, 255)  # 正常薄荷翠绿
    elif percent > 10:
        c_fill = (245, 158, 11, 255)  # 预警琥珀黄
    else:
        c_fill = (239, 68, 68, 255)   # 极低电量亮红

    # 电池外壳几何计算 (胶囊圆角比例)
    pad_y = int(3.5 * scale)
    tip_w = max(int(2.2 * scale), 4)
    tip_h = max(int(6.5 * scale), 10)

    bx0 = int(1.2 * scale)
    bx1 = W - tip_w - int(1.5 * scale)
    by0 = pad_y
    by1 = H - pad_y

    radius = int(3.0 * scale)
    frame_t = max(int(1.6 * scale), 2)

    # 1. 绘制外框微暗影底层（增强任务栏对比度）
    shadow_offset = int(0.6 * scale)
    draw.rounded_rectangle(
        [bx0, by0 + shadow_offset, bx1, by1 + shadow_offset],
        radius=radius,
        outline=c_shadow,
        width=frame_t
    )

    # 2. 绘制电池高光外框
    draw.rounded_rectangle([bx0, by0, bx1, by1], radius=radius, outline=c_frame, width=frame_t)

    # 3. 绘制正极凸起 (Tip)
    tx0 = bx1 - int(0.2 * scale)
    tx1 = W - int(0.8 * scale)
    ty0 = (H - tip_h) // 2
    ty1 = ty0 + tip_h
    draw.rounded_rectangle([tx0, ty0, tx1, ty1], radius=max(int(1.2 * scale), 2), fill=c_frame)

    # 4. 填充电池内部电量格
    in_x0 = bx0 + frame_t + 1
    in_x1 = bx1 - frame_t - 1
    in_y0 = by0 + frame_t + 1
    in_y1 = by1 - frame_t - 1
    in_w = in_x1 - in_x0
    in_h = in_y1 - in_y0

    if online and percent > 0:
        fill_ratio = min(max(percent / 100.0, 0.06), 1.0)
        fill_w = max(int(2 * scale), int(in_w * fill_ratio))
        # 内部充能微圆角
        draw.rounded_rectangle(
            [in_x0, in_y0, in_x0 + fill_w, in_y1],
            radius=max(int(1.5 * scale), 2),
            fill=c_fill
        )

    # 5. 文字与充电状态指示
    if online and percent >= 0:
        text = str(percent)
        # 字体大小按字符数自适应
        font_size = int(10.5 * scale) if len(text) <= 2 else int(8.8 * scale)
        try:
            # 优先使用 Windows 经典 ClearType 粗体字体
            font = ImageFont.truetype("segoeuib.ttf", font_size)
        except Exception:
            try:
                font = ImageFont.truetype("arialbd.ttf", font_size)
            except Exception:
                font = ImageFont.load_default()

        # 计算文字居中
        bbox = draw.textbbox((0, 0), text, font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]

        tx = in_x0 + (in_w - tw) // 2
        ty = in_y0 + (in_h - th) // 2 - int(0.5 * scale)

        # 文字阴影与正文（反白/反黑高对比）
        c_text = (255, 255, 255, 250) if (percent <= 30 and not charging) else (15, 23, 42, 240)
        c_halo = (0, 0, 0, 160) if c_text == (255, 255, 255, 250) else (255, 255, 255, 180)

        # 绘制 1px 光晕微描边保证极端背景下的字迹锐利
        for dx, dy in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
            draw.text((tx + dx, ty + dy), text, font=font, fill=c_halo)
        draw.text((tx, ty), text, font=font, fill=c_text)

        # 若处于充电中，右上角绘制矢量微型高光闪电
        if charging:
            bolt_size = int(9.5 * scale)
            bolt_cx = in_x1 - int(4 * scale)
            bolt_cy = by0 + int(3.5 * scale)
            # 闪电底圈光晕
            draw.ellipse(
                [bolt_cx - bolt_size//2 - 2, bolt_cy - bolt_size//2 - 2,
                 bolt_cx + bolt_size//2 + 2, bolt_cy + bolt_size//2 + 2],
                fill=(0, 80, 200, 220)
            )
            _draw_lightning_bolt(draw, bolt_cx, bolt_cy, bolt_size, color=(255, 255, 255, 255))
    else:
        # 离线展示优雅问号
        try:
            font = ImageFont.truetype("segoeuib.ttf", int(10 * scale))
        except Exception:
            font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), "?", font=font)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        tx = in_x0 + (in_w - tw) // 2
        ty = in_y0 + (in_h - th) // 2 - int(0.5 * scale)
        draw.text((tx, ty), "?", font=font, fill=(210, 220, 230, 240))

    # 超采样降采样到目标尺寸
    img = img.resize((size, size), Image.Resampling.LANCZOS)
    return img


def create_dual_device_tray_image(
    statuses: dict,
    size: int = 32
) -> Image.Image:
    """
    生成任务栏双设备（键盘 + 鼠标）并列微型电量条图标。
    支持 Windows 10/11 浅色/深色任务栏全天候自适应。
    - 左柱：键盘 (K)
    - 右柱：鼠标 (M)
    - 垂直充能柱 + 充电指示徽标 + 极高对比度描边
    """
    scale = 4
    W = size * scale
    H = size * scale

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 提取键盘与鼠标状态
    kb_st = statuses.get("lingbao_k98")
    ms_st = statuses.get("mchose_g3v2")

    # 若两个设备均不存在，尝试按 device_type 分类
    if not kb_st:
        kb_st = next((s for s in statuses.values() if getattr(s, "device_type", "") == "keyboard"), None)
    if not ms_st:
        ms_st = next((s for s in statuses.values() if getattr(s, "device_type", "") == "mouse"), None)

    dev_pairs = [
        ("K", kb_st),
        ("M", ms_st)
    ]

    # 单柱尺寸与布局
    col_w = int(10.5 * scale)
    gap = int(4.5 * scale)
    total_w = col_w * 2 + gap
    start_x = (W - total_w) // 2

    # 顶部标签预留高度
    tag_h = int(6.0 * scale)
    bar_y0 = int(7.5 * scale)
    bar_y1 = H - int(2.5 * scale)
    bar_h = bar_y1 - bar_y0
    radius = int(2.5 * scale)
    frame_t = max(int(1.2 * scale), 2)

    c_frame = (255, 255, 255, 240)
    c_shadow = (0, 0, 0, 120)
    c_track_bg = (20, 24, 33, 160)

    try:
        tag_font = ImageFont.truetype("segoeuib.ttf", int(6.5 * scale))
    except Exception:
        tag_font = ImageFont.load_default()

    for idx, (label, st) in enumerate(dev_pairs):
        bx0 = start_x + idx * (col_w + gap)
        bx1 = bx0 + col_w

        online = bool(st and st.online and st.percent >= 0)
        percent = st.percent if (st and st.percent >= 0) else -1
        charging = bool(st and st.charging)

        # 颜色计算
        if not online or percent < 0:
            c_fill = (130, 138, 150, 180)
            fill_ratio = 0.0
        elif charging:
            c_fill = (0, 160, 255, 255)
            fill_ratio = max(0.12, percent / 100.0)
        elif percent > 20:
            c_fill = (16, 185, 129, 255)
            fill_ratio = max(0.10, percent / 100.0)
        elif percent > 10:
            c_fill = (245, 158, 11, 255)
            fill_ratio = max(0.10, percent / 100.0)
        else:
            c_fill = (239, 68, 68, 255)
            fill_ratio = max(0.10, percent / 100.0)

        # 1. 绘制顶部文字标签 (K / M)
        bbox = draw.textbbox((0, 0), label, font=tag_font)
        tw = bbox[2] - bbox[0]
        tx = bx0 + (col_w - tw) // 2
        ty = int(0.5 * scale)
        # 标签光晕
        for dx, dy in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
            draw.text((tx + dx, ty + dy), label, font=tag_font, fill=(0, 0, 0, 180))
        label_color = (0, 210, 255, 255) if charging else ((255, 255, 255, 245) if online else (160, 170, 180, 200))
        draw.text((tx, ty), label, font=tag_font, fill=label_color)

        # 2. 槽底背景
        draw.rounded_rectangle([bx0, bar_y0, bx1, bar_y1], radius=radius, fill=c_track_bg)

        # 3. 内部电量填充（从下往上）
        if fill_ratio > 0:
            fh = int((bar_h - frame_t * 2) * fill_ratio)
            fy1 = bar_y1 - frame_t
            fy0 = max(bar_y0 + frame_t, fy1 - fh)
            fx0 = bx0 + frame_t
            fx1 = bx1 - frame_t
            draw.rounded_rectangle([fx0, fy0, fx1, fy1], radius=max(int(1.5 * scale), 1), fill=c_fill)

        # 4. 外边框与高反差投影
        draw.rounded_rectangle([bx0, bar_y0 + 1, bx1, bar_y1 + 1], radius=radius, outline=c_shadow, width=frame_t)
        draw.rounded_rectangle([bx0, bar_y0, bx1, bar_y1], radius=radius, outline=c_frame, width=frame_t)

        # 5. 若充电中，在柱身中间绘制微缩闪电
        if charging:
            bolt_cx = bx0 + col_w / 2.0
            bolt_cy = bar_y0 + bar_h / 2.0
            _draw_lightning_bolt(draw, bolt_cx, bolt_cy, size=int(8.5 * scale), color=(255, 255, 255, 255))

    img = img.resize((size, size), Image.Resampling.LANCZOS)
    return img


def create_dual_wx_icon(statuses: dict, size: int = 32) -> wx.Icon:
    """生成包含键盘与鼠标双设备电量的任务栏托盘 wx.Icon"""
    try:
        pil_img = create_dual_device_tray_image(statuses, size=size)
        wx_img = wx.Image(pil_img.width, pil_img.height)
        wx_img.SetData(pil_img.convert("RGB").tobytes())
        if "A" in pil_img.getbands():
            wx_img.SetAlpha(pil_img.getchannel("A").tobytes())

        bmp = wx.Bitmap(wx_img)
        icon = wx.Icon()
        icon.CopyFromBitmap(bmp)
        if icon.IsOk():
            return icon
    except Exception as e:
        print(f"[Icons] 双设备托盘图标异常: {e}")

    # 回退到单设备或默认
    valid_devs = [s for s in statuses.values() if s.online and s.percent > 0]
    if valid_devs:
        lowest = min(valid_devs, key=lambda s: s.percent)
        return create_wx_icon(lowest.percent, lowest.charging, True, lowest.device_type, size=size)
    return _fallback_icon(-1)


def create_wx_icon(percent: int, charging: bool = False, online: bool = True, device_type: str = "mouse", size: int = 16) -> wx.Icon:
    """将 PIL 图像转换为 wx.Icon 用于 Windows 系统托盘（含有效性校验与失败回退）"""
    try:
        pil_img = create_battery_icon_image(percent, charging, online, device_type, is_dark=True, size=size)

        # 转换为 wx.Bitmap
        wx_img = wx.Image(pil_img.width, pil_img.height)
        wx_img.SetData(pil_img.convert("RGB").tobytes())
        if "A" in pil_img.getbands():
            wx_img.SetAlpha(pil_img.getchannel("A").tobytes())

        bmp = wx.Bitmap(wx_img)
        icon = wx.Icon()
        icon.CopyFromBitmap(bmp)

        if not icon.IsOk():
            print(f"[Icons] wx.Icon 无效 (percent={percent} online={online})，回退纯色图标")
            return _fallback_icon(percent)
        return icon
    except Exception as e:
        import traceback
        print(f"[Icons] 生成托盘图标异常: {e}")
        traceback.print_exc()
        return _fallback_icon(percent)


def _fallback_icon(percent: int) -> wx.Icon:
    """纯色方块兜底图标（保证托盘永不显示问号）"""
    try:
        color = wx.Colour(16, 185, 129) if percent > 20 else wx.Colour(245, 158, 11)
        bmp = wx.Bitmap(16, 16, 32)
        dc = wx.MemoryDC(bmp)
        dc.SetBackground(wx.Brush(color))
        dc.Clear()
        dc.SelectObject(wx.NullBitmap)
        icon = wx.Icon()
        icon.CopyFromBitmap(bmp)
        return icon
    except Exception as e:
        print(f"[Icons] 兜底图标也失败: {e}")
        return wx.NullIcon

