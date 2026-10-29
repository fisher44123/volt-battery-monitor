# Volt · 外设电量监控

Windows 托盘小工具，读取两台 2.4G 无线外设的**真实硬件电量**，提供托盘常驻显示、控制中心面板、历史曲线与续航估算。

| 设备 | VID / PID | 协议 | 状态 |
|---|---|---|---|
| 凌豹 K98 机械键盘 | `0x3554` / `0xFA09` | compx | ✅ 真实电量可读 |
| 迈从 G3 V2 无线鼠标 | `0x3837` / `0x4245` | yjx | ✅ 与官方 MCHOSE HUB 共存免杀直读 |

技术栈：Python 3.13 + wxPython（托盘/窗口）+ Edge WebView2（界面渲染）+ hidapi（HID 通信）+ SQLite（历史统计）。

---

## 一、怎么跑起来（开发调试）

```bat
:: 1. 装依赖（只需一次）
C:\Python313\python.exe -m pip install -r requirements.txt

:: 2. 启动（有黑框能看日志）
C:\Python313\python.exe main.py
```

也可以直接双击 `start_debug.bat`（带控制台）或 `start.bat`（无控制台）。

**注意**：程序有单实例互斥锁，已在运行时再启动会唤醒旧窗口而不是开第二个。想强杀用 `stop.bat`。

## 二、怎么打包成 exe

```bat
C:\Python313\python.exe -m PyInstaller --noconfirm --clean --onefile --noconsole ^
  --icon "assets/app_icon.ico" --name Volt ^
  --collect-binaries wx ^
  --add-data "ui/assets;ui/assets" --add-data "assets;assets" --add-data "devices;devices" ^
  --exclude-module matplotlib --exclude-module numpy --exclude-module pandas ^
  --exclude-module scipy --exclude-module tkinter --exclude-module test ^
  --exclude-module unittest --exclude-module pydoc --exclude-module setuptools ^
  --exclude-module pip ^
  main.py
```

产物：`dist\Volt.exe`（约 30 MB）。

> **两个必须记住的坑**
> 1. 不要用 `--collect-all wx`。它会把 wxPython 的全部 dll（含调试版）一股脑塞进去，
>    是 exe 从 30MB 涨到 300MB 的主因。
> 2. 但**必须保留 `--collect-binaries wx`**。`WebView2Loader.dll` 是运行时用
>    `LoadLibrary` 动态加载的，PyInstaller 的依赖分析看不到它。少了这个 dll，
>    exe 能启动、日志也正常、**但界面一片空白**（WebView 句柄 = 0），
>    而且不会有任何报错 —— 这是最难查的一种故障。

## 三、数据都存在哪

| 路径 | 内容 |
|---|---|
| `data\volt.log` | 运行日志（带时间戳，超过 2MB 自动滚成 `volt.log.1`） |
| `data\battery_stats.db` | 历史电量 + 充电事件（SQLite，自动清理 90 天前明细） |
| `data\battery_cache.json` | 最后已知电量（设备休眠时兜底显示，界面会标注非实时） |
| `data\settings.json` | 用户设置（自启动、预警阈值等） |
| `devices\*.json` | 每台外设的协议配置（`enabled: true` 才会加载） |

打包后这些都在 exe 同级的 `data\` 下；`devices\` 会在首次运行时从 exe 内部自动复制出来。

## 四、目录结构

```
├── main.py                  入口：单实例锁 + wx.App + 日志落盘
├── config.py                路径、设置、开机自启动（注册表）
├── core/
│   ├── engine.py            设备引擎：3 秒总线对账 + 每设备常驻 HID 监听线程
│   ├── stats.py             SQLite 历史、放电速率、续航估算（单例连接 + 3 秒缓存）
│   ├── battery.py           BatteryStatus 数据类
│   └── sniffer.py           添加外设向导的 HID 嗅探
├── protocols/
│   ├── compx.py             凌豹 K98 协议
│   ├── yjx.py               迈从 G3 V2 协议
│   ├── rapoo.py / standard_hid.py   其它协议（备用）
│   └── registry.py          协议注册表
├── ui/
│   ├── webview_dashboard.py 控制中心主窗口（WebView2）
│   ├── tray.py              托盘图标 + 右键浅色卡片
│   ├── icons.py             PIL 动态绘制托盘图标
│   ├── osd.py               低电/充电的屏幕悬浮提示
│   ├── device_wizard.py     添加外设向导
│   └── assets/dashboard.html  前端界面（MCHOSE 浅色主题）
├── devices/                 设备配置
├── assets/                  图标与产品图
└── _archive/                历史调研残留（不入库，可随时删）
```

## 五、已知限制（不是 bug，是硬件事实）

1. **键盘/鼠标休眠时不刷新电量**。Dongle 拿不到休眠设备的回包，界面会保留最后已知值并标注 `(Cached)` / `💤休眠`，晃动一下即可刷新。
2. **电量不会精确到 1% 秒变**。设备固件通常只在整数百分比变化或特定事件上报，程序只是忠实转发。
3. **放电速率需要样本**。至少累计 10 分钟活跃放电且电量确实下降才会给出速率，否则显示 `--`（不编造数值）。

## 六、改动记录

### 2026-10-29 全面体检修复

**真 bug**
- 充电结束写入 SQL 用了 `UPDATE ... ORDER BY ... LIMIT`，CPython 内置 SQLite 不支持 → 抛异常并回滚整条记录。改为先取 id 再按主键更新。
- 凌豹缓存值缺少 `(Cached)` 标记，被引擎误判为实时真值。已补标记。
- 休眠帧 (`pct=0 + Sleeping`) 被 `percent > 0` 的判断整条丢弃，导致界面永远显示"待同步"。已放行。
- 界面的"放电速率"在算不出时硬编码 `0.8/1.2` 冒充真实值。已改为 `--`。
- 托盘右键卡片 `TrayCardPopup` 从未绑定 `EVT_TASKBAR_RIGHT_UP`，是死代码（实际弹的是原生菜单）。已接上。
- `yjx.py` 重复定义 `YjxProtocol` 类两次。已删除。
- 托盘"导出全部"把每台设备写进同一个 CSV，后者覆盖前者。改为一次性导出全部。

**性能**
- 界面每 400ms 跑 10 次 SQLite 查询（耗电速率被重复算两遍）→ 合并为 1 次 `get_ui_snapshot` + 3 秒缓存。
- 推送加了内容签名去重，数据没变不再向 WebView 注入 JSON。
- 总线对账 2 秒 → 3 秒；缓存写盘加脏检查；托盘图标加签名缓存；MCHOSE HUB 检测结果缓存 30 秒。
- 保活间隔 900s 与耗电统计的 600s 上限自相矛盾（保活记录全被当休眠剔除）→ 统一为 600s / 900s。

**打包**
- 移除从未使用的 matplotlib 依赖，去掉 `--collect-all wx`，加 `--exclude-module`。
- exe 体积 314MB → 30.5MB（-90%）。
- **踩坑记录**：去掉 `--collect-all wx` 后必须补 `--collect-binaries wx`，
  否则漏掉动态加载的 `WebView2Loader.dll`，exe 能启动但界面全白且无任何报错。

**工程**
- 初始化 git 仓库与 `.gitignore`。
- 30+ 个无关文件移入 `_archive/`。
- 日志改为追加写入 + 时间戳 + 自动滚动（原来每次启动清空）。
- 接活 `warning_threshold` / `critical_threshold` / `enable_logging` 三个原本无人读取的配置项。
- 导出结果改用界面内嵌提示，不再弹系统对话框。
