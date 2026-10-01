"""浅色主题的**设计系统**（配色、字阶、间距、圆角、样式表都集中在这里）。

## 配色语义（不是审美选择，是功能选择）

  RUNNING（计时中，正在消耗时长） → 红：这是**要避免**的状态
  PAUSED （已暂停，安全）        → 绿
  UNKNOWN（无法确认）            → 琥珀：既不敢当安全、也不当危险，一律按不安全处理

## 为什么把 token 单独列出来

旧版把颜色和字号直接写死在样式表串里，于是「标题 13px、按钮 11px、提示 10px」
这类数值散落各处，改一处就与别处不一致。现在所有视觉决策只在这里定义一次：

  · 字阶只允许 10 / 11 / 12 / 13 / 17 五档（够用且不散）
  · 间距只用 4 的倍数（4/6/8/10/12/14/18），保证纵向节奏一致
  · 圆角三档：卡片 14 · 控件 9 · 药丸全圆
  · 颜色分「中性阶（底/线/字）」与「语义色（三态 + 强调色）」

改视觉只改这一处，界面各处自动跟随。
"""
from __future__ import annotations

# ---------------------------------------------------------------- 中性阶
BG_PANEL = "#FFFFFF"
BG_SOFT = "#F7F8FA"          # 次级按钮底 / 分区底
BG_SOFT2 = "#EEF1F5"         # 按下态
BORDER = "#E7EAF0"           # 卡片描边（极浅）
BORDER_STRONG = "#D9DEE7"    # 控件描边
TEXT = "#15181E"             # 主文字
TEXT_SUB = "#59616E"         # 说明文字
TEXT_MUTE = "#98A0AC"        # 弱化：meta / 占位

# ---------------------------------------------------------------- 强调色
ACCENT = "#2B6CF6"
ACCENT_HI = "#4180F9"        # 渐变亮端
ACCENT_LO = "#1F5AE0"        # 渐变暗端
ACCENT_EDGE = "#1B4FD1"
ACCENT_SOFT = "#EAF1FE"
ACCENT_PRESSED_HI = "#3A74F5"
ACCENT_PRESSED_LO = "#1A4FCC"

# ---------------------------------------------------------------- 语义色
#: 每个状态给一组：前景（文字/点）/ 柔和底 / 描边。
#: 面板里凡是「与状态有关」的颜色都必须从这里取，不许就地写死十六进制 ——
#: 否则界面里会出现第四种红，用户就看不出哪个红才代表危险。
C_RUNNING = "#DE3B40"
C_PAUSED = "#0E9F4E"
C_UNKNOWN = "#C07700"
C_DANGER = "#DE3B40"          # 「允许本次关闭」这类危险动作

_STATE = {
    "RUNNING": {"label": "计时中", "fg": C_RUNNING, "soft": "#FEF1F2",
                "line": "#F8CED0", "dot": C_RUNNING},
    "PAUSED": {"label": "已暂停", "fg": C_PAUSED, "soft": "#EDF9F1",
               "line": "#C6EBD5", "dot": C_PAUSED},
    "UNKNOWN": {"label": "无法确认", "fg": C_UNKNOWN, "soft": "#FDF6E8",
                "line": "#F2DFB6", "dot": C_UNKNOWN},
    # 中性态：给「保护中/待命」这类**与时长状态无关**的药丸用。
    # 没有它的时候，保护状态只能借三态色来染 —— 结果「保护中」被画成绿色，
    # 而绿色在这一套界面里专指「时长已暂停」，用户会读错。
    "IDLE": {"label": "待命", "fg": TEXT_SUB, "soft": BG_SOFT,
             "line": BORDER_STRONG, "dot": TEXT_MUTE},
}

STATE_TIPS = {
    "running": "雷神总时长正在消耗。关闭雷神前本工具会先暂停时长。",
    "paused": "总时长已暂停，不会继续消耗。此时可以安全关闭雷神。",
    "unknown": "无法确认总时长状态。为防止时长被继续消耗，关闭操作会被阻止。",
}

# ---------------------------------------------------------------- 尺度
SP = 4                       # 间距基准
R_CARD = 14
R_CTL = 9
PAD_CARD = (14, 13, 14, 14)  # 卡片内边距 (左, 上, 右, 下)

#: 卡片四周留给投影的透明边。**改了它必须同步 follow() 的落位补偿**，
#: 否则面板与雷神窗口之间的视觉间距会跟着变（见 guard_window.follow）。
SHADOW_MARGIN = 9

#: 投影参数（Qt 的 QGraphicsDropShadowEffect）
SHADOW_BLUR = 22
SHADOW_DY = 5
SHADOW_COLOR = (17, 24, 39, 46)   # rgba，alpha 46/255 ≈ 18%


def state_style(state: str) -> tuple:
    """返回 (中文标签, 前景色, 柔和底色)。未知输入按 UNKNOWN 处理。"""
    d = state_tokens(state)
    return d["label"], d["fg"], d["soft"]


def state_tokens(state: str) -> dict:
    """返回该状态的完整配色字典（label/fg/soft/line/dot）。未知输入按 UNKNOWN。"""
    return dict(_STATE.get(str(state or "").upper(), _STATE["UNKNOWN"]))


# ---------------------------------------------------------------- 文案
def status_line(state: str, st: dict) -> str:
    """面板上那一行「大白话」。技术细节（识别证据）放悬停提示里，别糊到用户脸上。"""
    st = st or {}
    if not st.get("leigod_found"):
        return "未找到雷神窗口，保护尚未生效。"
    if st.get("pause_failed"):
        return (f"上次自动暂停失败（已尝试 {st.get('pause_fails', 0)} 次），"
                f"已保持拦截、不会自动放行。")
    s = str(state or "UNKNOWN").upper()
    if s == "RUNNING":
        return "正在监视中：关闭雷神前会先暂停总时长。"
    if s == "PAUSED":
        return "已确认总时长暂停，现在可以安全关闭雷神。"
    return "认不出时长按钮，已按「不安全」处理：关闭会被阻止。"


def protection_line(st: dict) -> str:
    """「关闭保护」那一行该说什么 —— 必须如实，不允许把失败说成没事。

    为什么单独抽出来：拦截是把雷神的系统菜单 SC_CLOSE 置灰。如果雷神以管理员
    身份运行而本程序没有，Windows 的 UIPI 会**静默拒绝**这次调用 —— 菜单其实
    没灰、✕ 其实还能点。旧写法在这种情况下会落到 else 分支显示「待命中」，
    等于告诉用户「一切正常」，这是本项目最不能接受的一种谎报。
    """
    st = st or {}
    # 保留「关闭保护：」这个前缀不是啰嗦，是**给第一次用的人指认这一行是什么**。
    # 旁边那个盾牌图标只有老用户才认得，不能拿它替代文字。
    if not st.get("leigod_found", True):
        return "关闭保护：待命中（尚未找到雷神）"
    if not st.get("protect_enabled", True):
        return "关闭保护：已关闭 —— 点 ✕ 会直接关闭雷神"
    if st.get("decision") == "ALLOW":
        return "关闭保护：已放行本次关闭"
    if st.get("sc_close_disabled"):
        return "关闭保护：已生效 · ✕ 已锁定"
    if str(st.get("decision") or "").startswith("BLOCK"):
        # 本该拦住却没拦住：必须让用户一眼看出「保护没在工作」。
        return "关闭保护：未能锁定 ✕（权限不足，请以管理员身份运行）"
    return "关闭保护：待命中"


def protection_tone(st: dict) -> str:
    """保护那一行该用哪种颜色：ok / warn / off / mute。

    为什么按「真的锁住了吗」而不是按「开关打开了吗」着色：开关是**意图**，
    SC_CLOSE 真的被置灰才是**事实**。用意图上色会把「未生效」画成安全色，
    是这套界面里最危险的一种视觉谎报。
    """
    st = st or {}
    if not st.get("leigod_found", True):
        return "mute"                       # 没有窗口 → 谈不上生效
    if not st.get("protect_enabled", True):
        return "off"
    if st.get("decision") == "ALLOW":
        return "ok"
    if st.get("sc_close_disabled"):
        return "ok"
    if str(st.get("decision") or "").startswith("BLOCK"):
        return "warn"
    return "mute"


TONE_COLOR = {
    "ok": C_PAUSED, "warn": C_DANGER, "off": TEXT_MUTE, "mute": TEXT_MUTE,
}


def protection_chip(st: dict) -> tuple:
    """标题右侧那颗药丸该显示什么。

    它回答的是**另一个问题**：不是「时长在不在走」（那是主卡片的职责），
    而是「关闭保护到底起没起作用」。两者混在一起用同一个词，用户会以为是同一件事。
    """
    st = st or {}
    # **没找到雷神**时保护不可能生效。这里必须先判它：否则 close 层给出的
    # decision 是 ALLOW（它没窗口可拦），界面就会显示绿色「保护中」——
    # 一个纯属虚构的安全信号。这是本界面里最不能出现的一类错。
    if not st.get("leigod_found", True):
        return "待命", "IDLE"
    if not st.get("protect_enabled", True):
        return "保护已关", "IDLE"
    tone = protection_tone(st)
    if tone == "ok":
        return "保护中", "PAUSED"
    if tone == "warn":
        return "未生效", "UNKNOWN"
    return "待命", "IDLE"


# ---------------------------------------------------------------- 样式表
def chip_qss(state: str) -> str:
    """标题右侧那颗状态药丸。"""
    t = state_tokens(state)
    return (f"background: {t['soft']}; color: {t['fg']};"
            f"border: 1px solid {t['line']}; border-radius: 9px;"
            f"padding: 2px 9px; font-size: 11px; font-weight: 600;")


def state_card_qss(state: str) -> str:
    """状态主卡片：底色随状态走，左缘一道强调色（一眼看出当前处于哪一态）。"""
    t = state_tokens(state)
    return (f"QFrame#stateCard {{ background: {t['soft']};"
            f" border: 1px solid {t['line']}; border-left: 3px solid {t['fg']};"
            f" border-radius: 10px; }}")


def notice_qss(strong: bool) -> str:
    """通知横幅：强提醒用危险色，一般提醒用中性色 + 强调色左缘。"""
    if strong:
        return (f"QFrame#notice {{ background: #FFF7F7; border: 1px solid #F6CFD1;"
                f" border-left: 3px solid {C_DANGER}; border-radius: 10px; }}")
    return (f"QFrame#noticeInfo {{ background: {BG_SOFT}; border: 1px solid {BORDER};"
            f" border-left: 3px solid {ACCENT}; border-radius: 10px; }}")


BASE_QSS = f"""
/* 面板背景画在**内层 QFrame** 上，不能只写 QWidget#panel。
   真机实测：面板 71% 的像素 alpha=0（连正中心都是透明的），用户看到的就是
   「背景透明、字看不清」。根因是 Qt 对**顶层普通 QWidget** 的样式表背景
   在 WA_TranslucentBackground 下不可靠地不绘制；QFrame 有自己的 frame 绘制
   路径，配上 WA_StyledBackground 才稳。外面那层透明窗口只负责圆角与投影。 */
QFrame#card {{
    background: {BG_PANEL};
    border: 1px solid {BORDER};
    border-radius: {R_CARD}px;
}}

QLabel, QPushButton, QCheckBox {{
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", sans-serif;
}}
QLabel {{ color: {TEXT}; }}
QLabel#title {{ font-size: 13px; font-weight: 600; letter-spacing: 0.2px; }}
QLabel#sub   {{ color: {TEXT_SUB}; font-size: 11px; }}
QLabel#meta  {{ color: {TEXT_MUTE}; font-size: 10px; }}
QLabel#state {{ font-size: 17px; font-weight: 600; }}
QLabel#stateSub {{ color: {TEXT_SUB}; font-size: 11px; }}
QLabel#hint  {{ color: {TEXT_MUTE}; font-size: 10px; }}
QLabel#noticeTitle {{ font-size: 12px; font-weight: 600; }}
QLabel#noticeBody  {{ color: {TEXT_SUB}; font-size: 11px; }}

/* 分隔线：极浅 1px，不用重色实线（小尺寸下会显得生硬） */
QFrame#sep {{ background: {BORDER}; border: none; max-height: 1px; }}

/* ---------------- 按钮 ---------------- */
QPushButton {{
    background: {BG_PANEL}; color: {TEXT};
    border: 1px solid {BORDER_STRONG}; border-radius: {R_CTL}px;
    padding: 7px 10px; font-size: 11px;
}}
QPushButton:hover {{ background: {BG_SOFT}; border-color: #C8CFDA; }}
QPushButton:pressed {{ background: {BG_SOFT2}; }}
QPushButton:disabled {{ color: {TEXT_MUTE}; background: #FAFBFC; border-color: #EDF0F4; }}
QPushButton:focus {{ border-color: {ACCENT}; }}

/* 主按钮：竖直渐变 + 深一档描边，比纯色块更「有实体」 */
QPushButton#primary {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                stop:0 {ACCENT_HI}, stop:1 {ACCENT_LO});
    color: #FFFFFF; border: 1px solid {ACCENT_EDGE};
    border-radius: {R_CTL}px; padding: 9px 12px;
    font-size: 12px; font-weight: 600;
}}
QPushButton#primary:hover {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                stop:0 #4B86FA, stop:1 #2563EB);
}}
QPushButton#primary:pressed {{
    background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                stop:0 {ACCENT_PRESSED_HI}, stop:1 {ACCENT_PRESSED_LO});
}}
/* 禁用态不要用"淡蓝 + 白字"：那是所有按钮里对比度最低的一种，
   看起来像没画完。改成中性灰，让"它现在不可用"这件事一眼可读。 */
QPushButton#primary:disabled {{ background: {BG_SOFT2}; border-color: {BORDER_STRONG};
                              color: {TEXT_MUTE}; font-weight: 500; }}

/* 危险动作：描边 + 浅底，不用实心红 —— 实心红会和「计时中」抢注意力 */
QPushButton#danger {{ color: {C_DANGER}; border-color: #F2CFD1; background: #FFF8F8; }}
QPushButton#danger:hover {{ background: #FDECEE; border-color: #EBBBBD; }}
QPushButton#danger:disabled {{ color: {TEXT_MUTE}; border-color: #EDF0F4; background: #FAFBFC; }}

/* 工具按钮：更小更弱，让主按钮保持唯一视觉焦点 */
QPushButton#tool {{
    background: transparent; border: 1px solid transparent;
    color: {TEXT_SUB}; padding: 5px 7px; font-size: 11px;
}}
QPushButton#tool:hover {{ background: {BG_SOFT}; border-color: {BORDER}; color: {TEXT}; }}
QPushButton#tool:pressed {{ background: {BG_SOFT2}; }}
QPushButton#tool:disabled {{ color: #C3C9D3; background: transparent; }}

/* 横幅里的次要动作（「知道了」这类） */
QPushButton#noticeGhost {{
    background: transparent; border: none; color: {TEXT_SUB};
    padding: 3px 6px; font-size: 11px;
}}
QPushButton#noticeGhost:hover {{ color: {TEXT}; background: {BG_SOFT}; border-radius: 6px; }}
"""
