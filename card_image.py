"""QQ 回复图片渲染：把 KOOK 的 KMarkdown 回复原样画成 PNG。

与 KOOK 端保持一致
────────────────
KOOK 频道里的回复是 KMarkdown 文本，本模块逐项还原：

| KMarkdown | 图片里的呈现 |
|---|---|
| `📂 ✅ ❌ ⚠️` 等 emoji | 彩色 emoji（用系统 emoji 字体渲染） |
| `**加粗**` | 加粗 + 更深颜色 |
| `> 引用行` | 左侧主题色竖线 + 淡色底 |
| ``` 代码块 ``` | 灰底块，保留对齐的空格 |
| `• 列表项` | 原样保留 |
| `(met)xxx(met)` | 去掉（KOOK 的 @提及语法，QQ 无意义） |

内容绝不丢失
────────────
- 标题、正文、页脚全部参与换行
- 超长不可断串（长 URL / 长英文串）按字符强制切分
- 宽度迭代收敛到内容真正需要的宽度；高度由行数累加，无上限
- 只有高度超过 MAX_AUTO_SHRINK 才缩字号（正常回复不触发）

字体
────
中文字体与 emoji 字体分开探测。任一缺失都会自动降级
（emoji 缺失就画成单色，中文缺失则返回 None 由调用方降级文本卡片）。
"""

import os
import re

try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    _PIL_OK = True
except Exception:  # pragma: no cover
    _PIL_OK = False


# ---------------------------------------------------------------- 字体探测
_FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
]

# 彩色 emoji 字体（Noto Color Emoji / Segoe UI Emoji 均支持 embedded_color）
_EMOJI_CANDIDATES = [
    # Windows：Segoe UI Emoji（Win10/11 自带，COLR 彩色）
    "C:/Windows/Fonts/seguiemj.ttf",
    # macOS
    "/System/Library/Fonts/Apple Color Emoji.ttc",
    "/Library/Fonts/Apple Color Emoji.ttc",
    # Linux 常见位置
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/opentype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/noto/NotoColorEmoji.ttf",
    # 单色兜底：至少能出形状，不会变豆腐块
    "C:/Windows/Fonts/seguisym.ttf",
    "/usr/share/fonts/truetype/ancient-scripts/Symbola_hint.ttf",
]

# 用来探测「这个字体到底能不能画出 emoji」的测试字符
_EMOJI_PROBE = "\U0001F600"  # 笑脸

_font_cache = {}
_emoji_cache = {}  # (char, size) -> RGBA Image


def find_font(user_path: str = ""):
    """中文字体路径；找不到返回 None"""
    if user_path:
        try:
            if os.path.exists(user_path):
                return user_path
        except Exception:
            pass
    for p in _FONT_CANDIDATES:
        try:
            if os.path.exists(p):
                return p
        except Exception:
            continue
    return None


def emoji_font_works(path) -> bool:
    """探测字体能否真正渲染出 emoji

    有些系统里字体文件存在，但 Pillow 版本不支持其彩色格式（如 COLR v1），
    渲染结果是空白 —— 这时不能拿它当 emoji 字体用，否则 emoji 全变空白。
    """
    global _EMOJI_PATH
    if not path:
        return False
    saved = _EMOJI_PATH
    try:
        _EMOJI_PATH = path
        _emoji_cache.clear()
        t = _emoji_tile(_EMOJI_PROBE, 32)
        if t is None:
            return False
        px = t.load()
        ink = sum(1 for y in range(t.size[1]) for x in range(t.size[0])
                  if px[x, y][3] > 30)
        return ink > 40  # 有墨迹才算真能画
    except Exception:
        return False
    finally:
        _emoji_cache.clear()
        _EMOJI_PATH = saved


def find_emoji_font(user_path: str = "", verify: bool = True):
    """emoji 字体路径；找不到或不可用返回 None

    verify=True 时会实际渲染一个测试 emoji 确认能画出来，
    避免「文件存在但 Pillow 不支持其彩色格式 → emoji 全空白」的坑。
    """
    tried = []
    if user_path:
        try:
            if os.path.exists(user_path):
                tried.append(user_path)
                if not verify or emoji_font_works(user_path):
                    return user_path
        except Exception:
            pass
    for p in _EMOJI_CANDIDATES:
        try:
            if os.path.exists(p):
                tried.append(p)
                if not verify or emoji_font_works(p):
                    return p
        except Exception:
            continue
    # 兜底：交给 fontconfig 找（彩色优先）
    try:
        import subprocess

        for name in ("Noto Color Emoji", "Apple Color Emoji", "Segoe UI Emoji", "emoji"):
            try:
                out = subprocess.run(
                    ["fc-match", "-f", "%{file}", name],
                    capture_output=True, text=True, timeout=5,
                )
                p = (out.stdout or "").strip()
                if p and os.path.exists(p) and p not in tried:
                    tried.append(p)
                    if not verify or emoji_font_works(p):
                        return p
            except Exception:
                continue
    except Exception:
        pass
    return None


def _get_font(path, size):
    key = (path, size)
    f = _font_cache.get(key)
    if f is not None:
        return f
    try:
        f = ImageFont.truetype(path, size)
    except Exception:
        f = ImageFont.load_default()
    _font_cache[key] = f
    return f


# ---------------------------------------------------------------- emoji 判定
_EMOJI_CHARS = (
    [(0x1F000, 0x1FAFF), (0x1F1E6, 0x1F1FF), (0x2600, 0x27BF),
     (0x2B00, 0x2BFF), (0x2300, 0x23FF), (0x2130, 0x214F),
     (0xFE00, 0xFE0F), (0x1F900, 0x1F9FF)]
)


def strip_emoji(s: str) -> str:
    """剥掉所有 emoji 字符

    只在【系统没有可用 emoji 字体】时才用它：
    中文字体没有 emoji 字形，硬画会变成「豆腐块」，
    与其显示一排方框，不如只保留文字。
    """
    out = []
    for ch in str(s or ""):
        cp = ord(ch)
        if 0xFE00 <= cp <= 0xFE0F or cp == 0x200D:
            continue  # 变体选择符 / ZWJ，依附 emoji，一并去掉
        if _is_emoji(ch):
            continue
        out.append(ch)
    s = "".join(out)
    return re.sub(r"[ \t]{2,}", " ", s).strip()


def _is_emoji(ch: str) -> bool:
    """判断单个字符是否应按 emoji 渲染"""
    if not ch or ch in ("\u200d",):
        return False
    cp = ord(ch[0])
    # 变体选择符 / ZWJ 不单独渲染，依附前一个字符
    if 0xFE00 <= cp <= 0xFE0F or cp == 0x200D or 0xE0020 <= cp <= 0xE007F:
        return False
    for lo, hi in _EMOJI_CHARS:
        if lo <= cp <= hi:
            return True
    return False


# Noto Color Emoji 是位图字体，109 是它的原生尺寸；渲染后缩放到目标大小
_EMOJI_NATIVE_SIZE = 109


def _emoji_tile(ch, size):
    """渲染一个 emoji 到 (size x size) 的 RGBA；失败返回 None

    ⚠️ 关键：NotoColorEmoji 是位图字体，必须按原生尺寸 109 渲染，
       得到的字形实际有 ~120×95 像素。画布若小于这个尺寸，字形会被
       裁掉一角 —— 放出来就是奇怪的色块碎片。所以画布必须给足。
    """
    key = (ch, size)
    tile = _emoji_cache.get(key)
    if tile is not None:
        return tile if tile is not False else None
    path = _EMOJI_PATH
    out = None
    if path:
        try:
            f = _get_font(path, _EMOJI_NATIVE_SIZE)
            # 画布要能完整装下 109 号字形（实测约 120×95），留 2 倍余量
            canvas_size = _EMOJI_NATIVE_SIZE * 2
            origin = _EMOJI_NATIVE_SIZE // 2
            canvas = Image.new("RGBA", (canvas_size, canvas_size), (0, 0, 0, 0))
            ImageDraw.Draw(canvas).text(
                (origin, origin), ch, font=f, embedded_color=True, fill=(0, 0, 0, 255)
            )
            bbox = canvas.getbbox()
            if bbox:
                glyph = canvas.crop(bbox)
                # 保持宽高比缩放（emoji 多半不是正方形，强行拉伸会变形）
                glyph.thumbnail((size, size), Image.LANCZOS)
                out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
                out.paste(glyph, ((size - glyph.width) // 2, (size - glyph.height) // 2))
            else:
                out = None
        except Exception:
            out = None
    _emoji_cache[key] = out if out is not None else False
    return out


_EMOJI_PATH = None  # 由 render_card 设置


# ---------------------------------------------------------------- KMarkdown 解析
_MET_RE = re.compile(r"\(met\)\d+\(met\)")
# KOOK 的字体染色语法：*(font)文字(font)[warning]* —— QQ 图片无法还原，剥掉标记保留文字
_FONT_RE = re.compile(r"\(font\)(.*?)\(font\)\[[^\]]*\]", re.S)

# 行内标记：**加粗** / `代码` / *斜体*
_INLINE_RE = re.compile(
    r"\*\*(?P<b>.+?)\*\*"          # **bold**
    r"|`(?P<c>[^`]+)`"                  # `code`
    r"|\*(?P<i>[^*\n]+?)\*"          # *italic*
    , re.S,
)


def _clean_kook(s: str) -> str:
    """去掉 KOOK 特有的、QQ 端无意义的语法标记"""
    s = _MET_RE.sub("", str(s or ""))
    s = _FONT_RE.sub(lambda m: m.group(1), s)
    # 残留的 (font) / (font)[xxx]
    s = s.replace("(font)", "")
    return s


def _parse_inline(line: str):
    """把一行解析成 [(text, style)]，style ∈ normal / bold / code

    同时清理 KOOK 的 (met)提及 与 (font)染色 语法，
    并把 `*斜体*` 转成正常文字（中文斜体不美观，直接去掉标记）。
    """
    s = _clean_kook(line)
    segs = []
    pos = 0
    for m in _INLINE_RE.finditer(s):
        if m.start() > pos:
            segs.append((s[pos:m.start()], "normal"))
        if m.group("b") is not None:
            segs.append((m.group("b"), "bold"))
        elif m.group("c") is not None:
            segs.append((m.group("c"), "code"))
        else:
            segs.append((m.group("i"), "normal"))
        pos = m.end()
    if pos < len(s):
        segs.append((s[pos:], "normal"))
    return segs or [("", "normal")]


def _parse_kmarkdown(text: str):
    """解析 KMarkdown → blocks

    返回 [{"type": "text"|"quote"|"code"|"blank", "segs": [...]}, ...]
    segs 是 [(text, style)]，style ∈ normal / bold / code
    """
    blocks = []
    in_code = False
    buf = []
    for raw in str(text or "").split("\n"):
        stripped = raw.strip()
        if stripped.startswith("```"):
            if in_code:
                if buf:
                    blocks.extend(buf)
                buf = []
                in_code = False
            else:
                if buf:
                    blocks.extend(buf)
                buf = []
                in_code = True
            continue
        if in_code:
            # 代码块内不做加粗解析，保留原始对齐空格
            blocks.append({"type": "code", "segs": [(raw, False)]})
            continue
        if not stripped:
            blocks.append({"type": "blank", "segs": [("", False)]})
            continue
        if stripped.startswith(">"):
            content = stripped.lstrip(">").strip()
            blocks.append({"type": "quote", "segs": _parse_inline(content)})
            continue
        blocks.append({"type": "text", "segs": _parse_inline(raw)})
    if buf:
        blocks.extend(buf)
    return blocks


# ---------------------------------------------------------------- 绘制上下文
class _Ctx:
    """负责：把 segs 切成 runs、测量宽度、折行、绘制"""

    def __init__(self, font_file, emoji_file, size, scale):
        self.size = size
        self.scale = scale
        self.f = _get_font(font_file, size)
        try:
            self.fb = _get_font(font_file, size)  # 粗体用描边模拟
        except Exception:
            self.fb = self.f
        self.emoji_size = max(8, round(size * 1.12))
        self._w_cache = {}

    def _char_w(self, ch):
        w = self._w_cache.get(ch)
        if w is None:
            try:
                w = self.f.getlength(ch)
            except Exception:
                w = self.size
            self._w_cache[ch] = w
        return w

    def segs_to_runs(self, segs):
        """[(text,style)] → [(kind, text, style)]，按 emoji 边界切分"""
        runs = []
        for text, style in segs:
            buf = ""
            buf_emoji = None
            for ch in text:
                is_e = _is_emoji(ch)
                # 变体选择符并入前一个 run
                if not is_e and (ord(ch) in range(0xFE00, 0xFE10) or ord(ch) == 0x200D):
                    buf += ch
                    continue
                if buf_emoji is None:
                    buf_emoji = is_e
                    buf = ch
                    continue
                if is_e == buf_emoji:
                    buf += ch
                else:
                    runs.append(("emoji" if buf_emoji else "text", buf, style))
                    buf_emoji = is_e
                    buf = ch
            if buf:
                runs.append(("emoji" if buf_emoji else "text", buf, style))
        return runs

    def run_width(self, run):
        kind, text, _ = run
        if kind == "emoji":
            return len(text) * self.emoji_size
        total = 0.0
        # 连续 ASCII 一起测量更准（含 kerning）
        buf = ""
        for ch in text:
            if ord(ch) < 128:
                buf += ch
            else:
                if buf:
                    total += self.f.getlength(buf)
                    buf = ""
                total += self._char_w(ch)
        if buf:
            total += self.f.getlength(buf)
        return total

    def wrap(self, segs, max_w):
        """折行 → [[runs], ...]，每行宽度 <= max_w"""
        runs = self.segs_to_runs(segs)
        if not runs:
            return [[]]
        # 把 run 再切成可断单元：ASCII 连续串整体、CJK 逐字、emoji 逐字
        units = []
        for kind, text, style in runs:
            if kind == "emoji":
                for ch in text:
                    units.append((kind, ch, style))
                continue
            buf = ""
            for ch in text:
                if ord(ch) < 128 and ch != " ":
                    buf += ch
                else:
                    if buf:
                        units.append((kind, buf, style))
                        buf = ""
                    if ch != " ":
                        units.append((kind, ch, style))
            if buf:
                units.append((kind, buf, style))

        lines = []
        cur = []
        cur_w = 0.0
        for u in units:
            uw = self.run_width(u)
            if uw > max_w and u[0] == "text" and len(u[1]) > 1:
                # 超宽不可断串 → 逐字符硬切
                if cur:
                    lines.append(cur)
                    cur, cur_w = [], 0.0
                piece = ""
                for ch in u[1]:
                    cw = self._char_w(ch)
                    if cur_w + cw > max_w and piece:
                        cur.append(("text", piece, u[2]))
                        lines.append(cur)
                        cur, cur_w, piece = [], 0.0, ""
                    piece += ch
                    cur_w += cw
                if piece:
                    cur.append(("text", piece, u[2]))
                continue
            if cur_w + uw > max_w and cur:
                lines.append(cur)
                cur, cur_w = [], 0.0
            cur.append(u)
            cur_w += uw
        if cur:
            lines.append(cur)
        return lines or [[]]

    def draw(self, canvas, d, x, y, runs, fill, bold_fill, code_fill=None):
        """绘制一行 runs（canvas 必须是 RGBA，emoji 才能正确粘贴）

        style: bold → 加粗色并描边加粗；code → 代码色；normal → 常规色
        """
        cx = x
        for kind, text, style in runs:
            if kind == "emoji":
                for ch in text:
                    if not _is_emoji(ch):
                        continue
                    tile = _emoji_tile(ch, self.emoji_size)
                    if tile is not None:
                        canvas.paste(tile, (round(cx), round(y)), tile)
                    else:
                        # 无 emoji 字体时降级：用中文字体单色画
                        d.text((cx, y + round(self.size * 0.08)), ch, font=self.f, fill=fill)
                    cx += self.emoji_size
                continue
            if not text:
                continue
            if style == "bold":
                col = bold_fill
            elif style == "code":
                col = code_fill or fill
            else:
                col = fill
            d.text((cx, y), text, font=self.f, fill=col)
            if style == "bold":
                # 轻微右移再画一次，模拟粗体
                d.text((cx + 0.8, y), text, font=self.f, fill=col)
            cx += self.f.getlength(text) if ord(text[0]) < 128 and len(text) > 1 and " " not in text else sum(
                self._char_w(c) for c in text
            )
        return cx


# ---------------------------------------------------------------- 配色
_TONES = {
    "success": (18, 183, 106),
    "error": (240, 68, 56),
    "warning": (247, 144, 9),
    "info": (46, 144, 250),
    "neutral": (102, 112, 133),
}

_BG_TOP = (238, 242, 247)
_BG_BOTTOM = (247, 248, 250)
_CARD = (255, 255, 255)
_TEXT = (26, 32, 44)
_TEXT_BOLD = (12, 16, 24)
_TEXT_CODE = (36, 92, 168)  # 行内代码用蓝灰，和正文区分
_TEXT_SUB = (102, 112, 133)
_LINE = (234, 236, 240)
_QUOTE_BG = (246, 248, 251)
_CODE_BG = (243, 245, 248)


def _draw_icon(d, cx, cy, r, kind, color):
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=tuple(color) + (255,))
    w = max(1, round(r * 0.22))
    if kind == "check":
        d.line([cx - r * 0.45, cy + r * 0.02, cx - r * 0.12, cy + r * 0.36], fill=(255, 255, 255, 255), width=w)
        d.line([cx - r * 0.12, cy + r * 0.36, cx + r * 0.48, cy - r * 0.34], fill=(255, 255, 255, 255), width=w)
    elif kind == "cross":
        d.line([cx - r * 0.34, cy - r * 0.34, cx + r * 0.34, cy + r * 0.34], fill=(255, 255, 255, 255), width=w)
        d.line([cx + r * 0.34, cy - r * 0.34, cx - r * 0.34, cy + r * 0.34], fill=(255, 255, 255, 255), width=w)
    elif kind == "bang":
        d.line([cx, cy - r * 0.42, cx, cy + r * 0.12], fill=(255, 255, 255, 255), width=w + 1)
        d.ellipse([cx - w, cy + r * 0.30, cx + w, cy + r * 0.30 + w * 2], fill=(255, 255, 255))
    else:
        d.ellipse([cx - w, cy - r * 0.44, cx + w, cy - r * 0.44 + w * 2], fill=(255, 255, 255))
        d.line([cx, cy - r * 0.14, cx, cy + r * 0.44], fill=(255, 255, 255, 255), width=w + 1)


_ICON_BY_TONE = {
    "success": "check", "error": "cross",
    "warning": "bang", "info": "info", "neutral": "info",
}


# ---------------------------------------------------------------- 布局
class _Layout:
    def __init__(self, title, body, foot, font_file, emoji_file, scale,
                 max_content_w, force_content_w=None):
        PAD = round(34 * scale)
        TITLE_SIZE = round(32 * scale)
        BODY_SIZE = round(24 * scale)
        FOOT_SIZE = round(18 * scale)
        ICON_R = round(16 * scale)
        GAP = round(13 * scale)
        self.PAD, self.ICON_R, self.GAP = PAD, ICON_R, GAP
        self.TITLE_SIZE, self.BODY_SIZE, self.FOOT_SIZE = TITLE_SIZE, BODY_SIZE, FOOT_SIZE
        self.scale = scale

        self.ctx_t = _Ctx(font_file, emoji_file, TITLE_SIZE, scale)
        self.ctx_b = _Ctx(font_file, emoji_file, BODY_SIZE, scale)
        self.ctx_f = _Ctx(font_file, emoji_file, FOOT_SIZE, scale)
        icon_block = ICON_R * 2 + GAP
        self.icon_block = icon_block

        self.min_content = round(360 * scale)
        self.max_content = max_content_w

        # ---- 宽度：迭代收敛 ----
        if force_content_w:
            self.content_w = int(max(self.min_content, min(self.max_content, force_content_w)))
        else:
            W = max_content_w
            for _ in range(8):
                widest = self._measure_all(title, body, foot, W, icon_block)
                new_w = int(max(self.min_content, min(self.max_content, widest)))
                if new_w >= W:
                    W = new_w
                    break
                W = new_w
                if W <= self.min_content:
                    break
            self.content_w = W

        # ---- 正式排版 ----
        self.title_lines = self.ctx_t.wrap(
            _parse_inline(title), max(60, self.content_w - icon_block)
        )
        self.body_lines = self._layout_body(body, self.content_w)
        self.foot_lines = []
        if foot:
            for raw in str(foot).split("\n"):
                if raw.strip():
                    self.foot_lines.extend(self.ctx_f.wrap(_parse_inline(raw), self.content_w))

        # ---- 行高 ----
        self.lh_title = round(TITLE_SIZE * 1.45)
        self.lh_body = round(BODY_SIZE * 1.68)
        self.lh_foot = round(FOOT_SIZE * 1.55)
        self.gap = round(20 * scale)
        self.gap_small = round(14 * scale)
        self.blank_h = round(self.lh_body * 0.5)

        self.card_w = self.content_w + PAD * 2

        # ---- 高度 ----
        h = PAD
        h += self.lh_title * len(self.title_lines)
        h += self.gap + 2
        if self.body_lines:
            h += self.gap
            for ln in self.body_lines:
                h += self._line_height(ln)
        if self.foot_lines:
            h += self.gap + 2 + self.gap_small
            h += self.lh_foot * len(self.foot_lines)
        h += PAD
        self.card_h = max(h, round(150 * scale))

    def _measure_all(self, title, body, foot, W, icon_block):
        widest = 0.0
        for i, runs in enumerate(self.ctx_t.wrap(_parse_inline(title), max(60, W - icon_block))):
            w = sum(self.ctx_t.run_width(r) for r in runs)
            widest = max(widest, w + (icon_block if i == 0 else 0))
        for ln in self._layout_body(body, W):
            ctx = self.ctx_t if ln["type"] in ("quote",) else self.ctx_b
            w = sum(ctx.run_width(r) for r in ln["runs"])
            indent = round(14 * (1 if ln["type"] in ("quote", "code") else 0))
            widest = max(widest, w + indent)
        if foot:
            for raw in str(foot).split("\n"):
                if raw.strip():
                    for runs in self.ctx_f.wrap(_parse_inline(raw), W):
                        widest = max(widest, sum(self.ctx_f.run_width(r) for r in runs))
        return widest

    def _layout_body(self, body, W):
        out = []
        for blk in _parse_kmarkdown(body):
            if blk["type"] == "blank":
                out.append({"type": "blank", "runs": []})
                continue
            ctx = self.ctx_t if blk["type"] == "quote" else self.ctx_b
            inner = W - round(28 if blk["type"] in ("quote", "code") else 0)
            for runs in ctx.wrap(blk["segs"], max(60, inner)):
                out.append({"type": blk["type"], "runs": runs})
        return out

    def _line_height(self, ln):
        if ln["type"] == "blank":
            return self.blank_h
        if ln["type"] == "quote":
            return self.lh_title + round(8 * self.scale)
        if ln["type"] == "code":
            return round(self.BODY_SIZE * 1.5) + round(4 * self.scale)
        return self.lh_body


# ---------------------------------------------------------------- 绘制
def _paint(lo, accent, scale, icon_kind):
    MARGIN = round(26 * scale)
    R = round(22 * scale)
    W = lo.card_w + MARGIN * 2
    H = lo.card_h + MARGIN * 2

    # ---- 背景（全程用 RGBA，emoji 才能用 paste 正确合成）----
    canvas = Image.new("RGBA", (W, H), _BG_TOP + (255,))
    bg = ImageDraw.Draw(canvas)
    for y in range(H):
        t = y / max(1, H - 1)
        bg.line([(0, y), (W, y)], fill=(
            round(_BG_TOP[0] + (_BG_BOTTOM[0] - _BG_TOP[0]) * t),
            round(_BG_TOP[1] + (_BG_BOTTOM[1] - _BG_TOP[1]) * t),
            round(_BG_TOP[2] + (_BG_BOTTOM[2] - _BG_TOP[2]) * t),
            255,
        ))

    # ---- 卡片阴影 ----
    off = round(6 * scale)
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [MARGIN - 4, MARGIN - 2 + off, MARGIN + lo.card_w + 4, MARGIN + lo.card_h + 12],
        radius=R, fill=(15, 23, 42, 46),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(round(11 * scale)))
    canvas.alpha_composite(shadow)

    # ---- 卡片本体 + 顶部主题色条 ----
    mask = Image.new("L", (lo.card_w, lo.card_h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, lo.card_w - 1, lo.card_h - 1], radius=R, fill=255)
    card = Image.new("RGBA", (lo.card_w, lo.card_h), (255, 255, 255, 0))
    ImageDraw.Draw(card).rounded_rectangle([0, 0, lo.card_w - 1, lo.card_h - 1], radius=R, fill=_CARD + (255,))
    bar_h = round(7 * scale)
    bar = Image.new("RGBA", (lo.card_w, lo.card_h), (0, 0, 0, 0))
    ImageDraw.Draw(bar).rectangle([0, 0, lo.card_w, bar_h], fill=accent + (255,))
    card.alpha_composite(Image.composite(bar, Image.new("RGBA", (lo.card_w, lo.card_h), (0, 0, 0, 0)), mask))
    canvas.alpha_composite(card, (MARGIN, MARGIN))

    # ---- 卡片内的 quote / code 背景也画在 RGBA 层上 ----
    inner = Image.new("RGBA", (lo.card_w, lo.card_h), (0, 0, 0, 0))
    idraw = ImageDraw.Draw(inner)

    d = ImageDraw.Draw(canvas)
    x0 = MARGIN + lo.PAD
    x_end = MARGIN + lo.card_w - lo.PAD
    # 文字绘制代理：写进 inner 层，最后再合成到卡片上（保证被圆角裁剪）
    y = MARGIN + lo.PAD

    # ---- 标题 ----
    for i, runs in enumerate(lo.title_lines):
        cx = x0
        if i == 0:
            _draw_icon(d, x0 + lo.ICON_R, y + round(lo.TITLE_SIZE * 0.62), lo.ICON_R, icon_kind, accent)
            cx = x0 + lo.icon_block
        lo.ctx_t.draw(canvas, d, cx, y, runs, _TEXT, _TEXT_BOLD, _TEXT_CODE)
        y += round(lo.TITLE_SIZE * 1.45)

    y += lo.gap
    d.line([(x0, y), (x_end, y)], fill=_LINE + (255,), width=2)
    y += 2

    # ---- 正文 ----
    if lo.body_lines:
        y += lo.gap
        i = 0
        n = len(lo.body_lines)
        while i < n:
            ln = lo.body_lines[i]
            if ln["type"] == "blank":
                y += lo.blank_h
                i += 1
                continue
            if ln["type"] in ("quote", "code"):
                j = i
                while j < n and lo.body_lines[j]["type"] == ln["type"]:
                    j += 1
                grp = lo.body_lines[i:j]
                top = y
                h = sum(lo._line_height(g) for g in grp)
                indent = round(14 * scale)
                if ln["type"] == "quote":
                    # 引用块：左侧主题色竖线 + 淡底
                    inner_bg = Image.new("RGBA", (lo.card_w, lo.card_h), (0, 0, 0, 0))
                    ImageDraw.Draw(inner_bg).rectangle(
                        [lo.PAD, top - MARGIN, lo.card_w - lo.PAD, top - MARGIN + h],
                        fill=_QUOTE_BG + (255,),
                    )
                    ImageDraw.Draw(inner_bg).rectangle(
                        [lo.PAD, top - MARGIN, lo.PAD + round(4 * scale), top - MARGIN + h],
                        fill=accent + (255,),
                    )
                    inner.alpha_composite(inner_bg)
                else:
                    # 代码块：灰底
                    inner_bg = Image.new("RGBA", (lo.card_w, lo.card_h), (0, 0, 0, 0))
                    ImageDraw.Draw(inner_bg).rectangle(
                        [lo.PAD, top - MARGIN, lo.card_w - lo.PAD, top - MARGIN + h],
                        fill=_CODE_BG + (255,),
                    )
                    inner.alpha_composite(inner_bg)
                yy = top + (round(4 * scale) if ln["type"] == "code" else round(5 * scale))
                for g in grp:
                    ctx = lo.ctx_t if ln["type"] == "quote" else lo.ctx_b
                    ctx.draw(inner, idraw, x0 + indent - MARGIN, yy - MARGIN, g["runs"], _TEXT, _TEXT_BOLD, _TEXT_CODE)
                    yy += lo._line_height(g)
                y = top + h
                i = j
                continue
            lo.ctx_b.draw(inner, idraw, x0 - MARGIN, y - MARGIN, ln["runs"], _TEXT, _TEXT_BOLD, _TEXT_CODE)
            y += lo.lh_body
            i += 1

    # ---- 页脚 ----
    if lo.foot_lines:
        y += lo.gap
        d.line([(x0, y), (x_end, y)], fill=_LINE + (255,), width=2)
        y += 2 + lo.gap_small
        for runs in lo.foot_lines:
            lo.ctx_f.draw(inner, idraw, x0 - MARGIN, y - MARGIN, runs, _TEXT_SUB, _TEXT_SUB, _TEXT_SUB)
            y += lo.lh_foot

    # 把文字层合成到卡片（用 mask 裁掉圆角外溢出）
    clipped = Image.new("RGBA", (lo.card_w, lo.card_h), (0, 0, 0, 0))
    clipped.paste(Image.composite(inner, Image.new("RGBA", inner.size, (0, 0, 0, 0)), mask), (0, 0))
    canvas.alpha_composite(clipped, (MARGIN, MARGIN))

    return canvas.convert("RGB")


_MAX_AUTO_SHRINK = 8000


def render_card(
    title: str,
    body: str = "",
    footer: str = None,
    tone: str = "info",
    font_path: str = "",
    emoji_font_path: str = "",
    max_width: int = 660,
):
    """返回 PIL.Image；环境不支持（无 Pillow / 无中文字体）返回 None"""
    global _EMOJI_PATH
    if not _PIL_OK:
        return None
    font_file = find_font(font_path)
    if not font_file:
        return None
    _EMOJI_PATH = find_emoji_font(emoji_font_path)

    # ⚠️ 系统没有可用 emoji 字体时，中文字体会把 emoji 画成「豆腐块」。
    #    与其显示一排方框，不如剥掉 emoji 只保留文字。
    if _EMOJI_PATH is None:
        title = strip_emoji(title) or "结果"
        body = strip_emoji(body)
        footer = strip_emoji(footer) if footer else ""

    accent = _TONES.get(tone, _TONES["info"])
    icon_kind = _ICON_BY_TONE.get(tone, "info")

    base_w = None
    for scale in (1.0, 0.9, 0.8, 0.72, 0.64, 0.56):
        try:
            lo = _Layout(title, body, footer, font_file, _EMOJI_PATH, scale,
                         max_width, force_content_w=base_w)
            if base_w is None:
                base_w = lo.content_w
            if lo.card_h + round(26 * scale) * 2 <= _MAX_AUTO_SHRINK or scale == 0.56:
                return _paint(lo, accent, scale, icon_kind)
        except Exception:
            if scale == 0.56:
                return None
    return None


def save_card(img, out_dir, name_hint="card"):
    try:
        os.makedirs(out_dir, exist_ok=True)
        import time
        import uuid

        fn = f"kb_{name_hint}_{int(time.time())}_{uuid.uuid4().hex[:6]}.png"
        p = os.path.join(out_dir, fn)
        img.save(p, format="PNG", optimize=True)
        return p
    except Exception:
        return None
