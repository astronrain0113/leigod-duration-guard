# 旧版 ThunderGuard 代码审计报告

> 依据规格书 **三十二、非常重要：先审计现有代码**：
> 「如果已有 Agent 写出的版本，不许直接堆代码，必须先读整个项目……然后给出
> IMPLEMENTED / PARTIAL / BROKEN / MISSING 四种状态。架构本身错误就直接重构。」
>
> 审计对象：`C:\Users\<用户>\Desktop\workbuddy\ThunderGuard\`（旧实现）
> 审计结论：**架构不可修补地偏离规格书 → 重构为新项目 `leigod_duration_guard/`**
> 审计方式：全量阅读源码 + 读取其运行日志 `logs/thunderguard.log` + 查看其截图证据
> （`logs/thunder_window.png`、`logs/btn_probe.png`、`logs/frames/*.png`）

---

## 一、结论总表

| 功能 | 状态 | 关键证据 |
|---|---|---|
| 进程 / 窗口检测 | **IMPLEMENTED** | `detector/thunder_detector.py`，能按进程名找窗口 |
| 空闲检测 | **IMPLEMENTED** | `core/usage_tracker.py`、`detector/idle_detector.py` |
| 使用统计 | **IMPLEMENTED** | `logs/usage.db`（SQLite） |
| 开机自启 | **IMPLEMENTED** | `core/autostart.py` |
| 加速状态识别（三态） | **BROKEN** | `accel_state.py:112` 仅在前台截图、`ImageGrab` 可截到遮挡画面、无截图有效性校验 |
| UIA 暂停策略 | **BROKEN** | `pause_controller.py:17/176-179` 子串匹配 + **点击即宣布成功**（详见 §2.1） |
| 坐标校准 | **PARTIAL** | `calibrate.pyw:214` 主动传入 `verify=False`，校准恒定「成功」（详见 §2.2） |
| **关闭保护（第一核心功能）** | **MISSING** | 全项目搜索 `SC_CLOSE` / `GetSystemMenu` / `EnableMenuItem` / `WM_CLOSE` / `HTCLOSE` —— **零命中** |
| UI Inspector（诊断工具） | **MISSING** | 无任何控件树 / HWND / DPI / 截图导出工具 |
| 游戏监控 | **PARTIAL** | `game_detector.py` 有进程匹配，但无「退出后等待 N 秒且期间不得重启」的取消逻辑 |
| 启动器 | **PARTIAL** | `launcher.pyw` 只负责拉起进程，不等待主窗口、不做单实例 |
| UI 伴生 | **PARTIAL** | `gui.py` 是左右停靠的**固定面板**，不跟随雷神窗口；日志里多次出现「未找到加速器窗口」 |
| 配置 / 日志 | **PARTIAL** | 有 JSON 与日志，但日志未做敏感信息脱敏；配置文件语义与取值不一致（见 §2.5） |
| 违背规格书二：禁止 API 方案 | **违规** | `pause_controller.py:142-156` 有 `LocalApiStrategy`，且被排在策略链**第一位**（详见 §2.4） |

---

## 二、关键缺陷详证

### 2.1 UIA 暂停：子串匹配点错控件 + 「点击成功即成功」（最严重）

```python
# controller/pause_controller.py
17:  PAUSE_HINTS = ("暂停", "停止加速", "停止")          # ← 裸子串
...
176: for btn, _ in auto.WalkControl(win, auto.ControlType.ButtonControl, maxDepth=8):
177:     if any(h in (btn.Name or "") for h in PAUSE_HINTS):
178:         btn.Click(simulateMove=False)
179:         return PauseResult(True, self.name, f"已点击按钮: {btn.Name}")   # ← 点完就报成功
```

两处致命问题：

1. **`h in btn.Name` 是子串匹配**。雷神设置页里的「**自动暂停延迟**：」包含「暂停」，
   于是被当成暂停按钮点掉。运行日志给出了直接证据：
   ```
   logs/thunderguard.log:
     手动暂停: UIAutomation - 已点击按钮: 自动暂停延迟：
   ```
   → 每一次「自动暂停」都在点一个无关的设置项，总时长状态永远不变。

2. **`return PauseResult(True, ...)` 只表示「点下去了」**，没有重新读状态。
   这正是规格书 **十六、绝对禁止的逻辑** 里点名禁止的写法
   （`if click_success: allow_close()` 的变体）。

结论：这条链路同时踩了规格书第七（UIA 需精确匹配）与第十/第十六（点击 ≠ 暂停成功）两条红线，
且**这是它唯一「成功」的暂停路径**——所以旧版的自动暂停功能实际上从未生效过。

### 2.2 坐标校准：主动关闭验证，校准恒定成功

```python
# calibrate.pyw
214:            verify=False)
```

规格书 **二十五、坐标校准工具重新设计** 的硬性要求是：
「校准完成后必须立即执行一次真实测试……如果没有：Calibration FAILED，
**绝对不能显示 Calibration SUCCESS**。」
旧实现把验证参数写成 `False`，等于把这条要求直接删掉：
用户可以「校准成功」，但主程序按该坐标点下去毫无效果，且没有任何提示。
注：被调用的 `robust_click` 本身**是带 `_ocr_state` 复核的**
（`pause_controller.py:124-131`），是被调用方主动关掉的 —— 属于「自己拆掉自己的保险」。

### 2.3 关闭保护完全缺失

对整个旧项目（`*.py/*.pyw/*.json/*.toc`）搜索以下关键字：

```
SC_CLOSE  /  GetSystemMenu  /  EnableMenuItem  /  WM_CLOSE  /  HTCLOSE
```

**零命中。**

也就是说规格书 **十二、整个项目最重要的功能：关闭保护** 与
**十三、关闭事件的实现方式** 在旧实现里**一行都没有**。
这不是「做得不好」，而是「没做」——第一核心功能缺失，项目定位（保险丝）不成立。

### 2.4 策略链第一位是本地 API（违反规格书二）

```python
# controller/pause_controller.py
142: class LocalApiStrategy:
145:     def try_pause(self, ctx):
146:         endpoint = ctx.config.get("api_endpoint")
149:         import requests
151:         resp = requests.post(endpoint, timeout=2)
...
221:     STRATEGIES = (LocalApiStrategy, UiaStrategy, MouseClickStrategy)   # ← API 排第一
```

规格书 **二、最重要的设计原则**：禁止使用雷神 API 作为核心方案。
客观地说：该策略在 `api_endpoint` 未配置时会「跳过」，因此默认配置下并未真正发请求；
但把它放在策略链**第一位**，意味着一旦有人填了地址，暂停动作就变成网络请求优先——
这与规格书要求的「以本地 UI 与窗口机制为主要信息来源和控制手段」方向相反。
**定性：违规（潜在风险已实现为代码路径）。**

### 2.5 配置文件语义与取值不一致

```json
// config.json:38
"thunder_exe_path": "D:\\LeiGod_Acc\\leigod_launcher.exe"
```
```python
// core/config.py:38
"thunder_exe_path": "",   # 雷神主程序路径（launcher.pyw 使用）
```

注释说「主程序路径」，填的却是 **启动器**。雷神的 `leigod_launcher.exe` 会另起
`leigod.exe`，两者都含 `leigod` 字符串；旧代码用子串匹配
（`thunder_detector.py:147`、`watcher.pyw:45`、`game_detector.py:47`）
会同时命中两者，主窗口归属不确定 —— 这是「找不到窗口 / 绑定错窗口」的根源之一。

### 2.6 状态识别：截图不可信、无有效性校验

```python
# detector/accel_state.py
107:  PrintWindow 被 UIPI 拦截（雷神提权后跨进程绘图无效），
112:  if win32gui.GetForegroundWindow() != hwnd:      # 只在前台才截屏
118:  from PIL import ImageGrab
121:  img = np.asarray(ImageGrab.grab(bbox=(l, t, r, b), all_screens=True))[:, :, ::-1]
```

问题：`ImageGrab` 抓的是**屏幕该矩形上的内容**，谁压在上面就抓到谁。
旧实现的证据文件正好记录了这个后果：

- `logs/btn_probe.png`、`logs/frames/classify_strip.png`：截到的**不是雷神顶栏**，
  而是被遮挡的游戏画面与弹幕；
- 于是 OCR 得到无关文字，状态判定自然失真，并且**没有任何环节发现这张图是脏的**。

新实现对此新增了硬性关卡：`detection/image_detection.looks_like_real_capture()`
（全黑 / 全白 / std 过低 / 尺寸不符一律判为不可信，直接产出 UNKNOWN 而不是去猜）。

---

## 三、为什么选择重构而不是修补

1. **定位性缺失**：第一核心功能（关闭保护）零实现。补它需要新增窗口消息拦截、
   三级识别、三态状态机、证据合成、关闭策略——等同于新建主干，而不是打补丁。
2. **错误已固化在架构里**：
   - 「点击即成功」写在策略返回值里，`PauseController.pause()` 直接采信它
     （`pause_controller.py:226-234`），上层没有任何复核机会；
   - 识别层只产出「一个状态」，没有「证据链 + 冲突检测」的概念，
     无法满足规格书第九「结果冲突 = UNKNOWN」的要求；
   - 坐标校准与主程序点击**不共用同一套换算**（校准写 ratio/pos，
     主程序另有一份点击位置计算），校准通过 ≠ 主程序点得准。
3. **分层不满足可测试性要求**：业务逻辑与 Qt 界面耦合在 `main.py`（17.5KB），
   无法做到规格书第三十三「不许假装成功」所要求的自动化验证。

新项目 `leigod_duration_guard/` 的对应重构：

| 旧版问题 | 新实现的对应设计 |
|---|---|
| 点击即成功 | `PauseOutcome` 只由**重新识别结果**决定；`DurationController.pause()` 重试前必重读状态 |
| 单一状态 | `Evidence` / `Reading` / `combine_states()` —— 冲突即 UNKNOWN，且证据可追溯 |
| 无关闭保护 | `close_protection.py` + 实测有效的系统菜单锁定 + 低层钩子识别关闭意图 |
| 校准不可信 | `diagnostics/calibration.py` 强制真实点击测试，未通过不写配置 |
| 脏图猜状态 | `image_detection.looks_like_real_capture()` 前置校验 |
| 子串误匹配 | `ui_automation.classify_control_name()` 精确匹配（附回归测试） |
| UI/逻辑耦合 | `core/protection_engine.py` **零 Qt 依赖**，UI 只通过 sink 订阅事件 |
| 无诊断工具 | `diagnostics/window_inspector.py` + `ui_inspector.py`（控件树/截图/OCR/结论） |

---

## 四、旧版中保留价值的资产

审计不是全盘否定。以下内容被确认为有用，已提取其**结论**（而非代码）沿用：

1. **真实雷神 UI 的实测截图**（`logs/thunder_window.png`、`logs/frames/topbar_zoom.png`）
   确认了顶栏右侧顺序：`[剩余时长] [开启时长|暂停时长] [充值] [≡] [−] [✕]`，
   以及两种按钮配色（RUNNING 红底白字 / PAUSED 白底深字）。新实现的默认 OCR 裁剪区域
   与颜色辅助判定都以此为依据。
2. **「PrintWindow 在雷神提权时被 UIPI 拦截」这条实测记录**，
   直接启发了新实现的截图有效性校验与权限匹配自检。
3. **使用统计、空闲检测、开机自启** 三个模块功能正常，按规格书属于「不影响核心」的辅助能力，
   新项目保留等价配置项（`general.auto_start`），不重复实现其业务逻辑。

---

## 五、遗留风险提示

旧版 ThunderGuard 在本次审计期间**仍在后台运行**
（实测 PID 40928，`pythonw.exe C:\Users\<用户>\Desktop\workbuddy\ThunderGuard\main.py`，
窗口「雷神时长保护助手」隐藏到托盘）。

它会与新版同时争抢雷神窗口的关注（例如重复改窗口状态、抢占前台），
**在进行真机验证前必须先把它彻底退出**（托盘图标右键退出，或结束该进程）。
本报告不对该程序的取舍做决定，只做事实陈述。
