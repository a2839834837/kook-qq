import asyncio
import base64
import json
import os
import re
import tempfile
import unicodedata

import aiohttp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:  # AstrBot >= 3.5 推荐使用数据目录
    from astrbot.api.star import StarTools

    _HAS_STAR_TOOLS = True
except Exception:  # pragma: no cover
    _HAS_STAR_TOOLS = False


@register("kook_bridge", "唛头", "把 QQ 消息转发给本地 KOOK 机器人执行频道指令", "1.0.0")
class KookBridgePlugin(Star):
    """QQ ↔ KOOK 指令桥

    QQ 侧发「kook <指令>」，插件把指令 POST 给本地 KOOK 机器人，
    机器人复用自身逻辑在频道里执行，并把回复文本回传 QQ。
    """

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self.base_url = str(self.config.get("bridge_url", "http://127.0.0.1:8787")).rstrip("/")
        self.token = str(self.config.get("bridge_token", "") or "").strip()
        self.guild_id = str(self.config.get("kook_guild_id", "") or "")
        self.channel_id = str(self.config.get("kook_channel_id", "") or "")
        self.default_kook_user = str(self.config.get("default_kook_user", "") or "")
        self.async_mode = bool(self.config.get("async_mode", True))
        self.echo_to_kook = bool(self.config.get("echo_to_kook", True))
        self.timeout = int(self.config.get("timeout_sec", 120) or 120)
        self.admin_only = bool(self.config.get("admin_only", False))
        # 仅私聊生效：开启后忽略群消息，只处理私聊里的指令
        self.private_only = bool(self.config.get("private_only", False))
        self.allow_qq = {str(x).strip() for x in (self.config.get("allow_qq") or []) if str(x).strip()}
        self.bindings = self._load_bindings()
        # 持有异步任务的强引用，防止被 GC 回收导致结果丢失
        self._tasks: set = set()
        # 读一次框架的唤醒前缀，用于给用户输入提示（读不到就用 "/"）
        try:
            wp = self.context.get_config().get("wake_prefix") or ["/"]
        except Exception:
            wp = ["/"]
        self.wake_prefix = (wp[0] if isinstance(wp, list) and wp else str(wp)) or "/"

        # ---- 图片回复 ----
        self.image_mode = bool(self.config.get("image_mode", True))
        self.font_path = str(self.config.get("image_font_path", "") or "")
        self.emoji_font_path = str(self.config.get("image_emoji_font_path", "") or "")
        self.image_dir = self._init_image_dir()
        self._card_image = None  # 惰性加载
        self._img_ready = None

    # ---------------- 图片回复 ----------------
    def _init_image_dir(self) -> str:
        """图片输出目录：优先 AstrBot 数据目录，否则系统临时目录"""
        try:
            if _HAS_STAR_TOOLS:
                d = os.path.join(str(StarTools.get_data_dir()), "imgs")
            else:
                d = os.path.join(tempfile.gettempdir(), "astrbot_kook_bridge_imgs")
        except Exception:
            d = os.path.join(tempfile.gettempdir(), "astrbot_kook_bridge_imgs")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
        return d

    def _get_card_image(self):
        """加载同目录下的 card_image.py（用 importlib，避免包导入方式差异）"""
        if self._card_image is not False and self._card_image is None:
            try:
                import importlib.util
                from pathlib import Path

                p = Path(__file__).with_name("card_image.py")
                spec = importlib.util.spec_from_file_location("kook_bridge_card_image", str(p))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                self._card_image = mod
                font = mod.find_font(self.font_path)
                if not font:
                    logger.warning("[kook_bridge] 未找到中文字体，图片回复将降级为文本卡片")
                else:
                    efont = mod.find_emoji_font(self.emoji_font_path)
                    logger.info(f"[kook_bridge] 图片卡片已就绪，中文字体：{font}")
                    logger.info(
                        f"[kook_bridge] emoji 字体：{efont or '未找到（emoji 将显示为单色）'}"
                    )
            except Exception as e:
                logger.warning(f"[kook_bridge] 图片模块不可用，降级为文本卡片：{e}")
                self._card_image = False
        return self._card_image or None

    def _render(self, title, body="", footer=None, tone="info", plain=None):
        """返回 (文本兜底, 图片路径|None)

        文本兜底用清理掉 KMarkdown 标记的纯文本，避免出现 ** 和 ` 这类符号
        """
        if plain is None:
            plain = self._to_plain(body)
        foot_plain = self._to_plain(footer) if footer else None
        text = self._card(self._to_plain(title), plain, foot_plain)
        if not self.image_mode:
            return text, None
        ci = self._get_card_image()
        if not ci:
            return text, None
        try:
            img = ci.render_card(
                title, body, footer, tone,
                font_path=self.font_path,
                emoji_font_path=self.emoji_font_path,
            )
            if img is None:
                return text, None
            path = ci.save_card(img, self.image_dir)
            if not path:
                return text, None
            self._cleanup_images()
            return text, path
        except Exception as e:
            logger.warning(f"[kook_bridge] 生成图片失败，降级文本：{e}")
            return text, None

    def _cleanup_images(self, keep: int = 40):
        """只保留最近若干张，避免临时图片堆积"""
        try:
            files = [
                os.path.join(self.image_dir, f)
                for f in os.listdir(self.image_dir)
                if f.startswith("kb_") and f.endswith(".png")
            ]
            if len(files) <= keep:
                return
            files.sort(key=lambda p: os.path.getmtime(p))
            for p in files[:-keep]:
                try:
                    os.remove(p)
                except Exception:
                    pass
        except Exception:
            pass

    def _emit(self, event, title, body="", footer=None, tone="info", plain=None):
        """构造回复：能出图就出图，否则文本卡片"""
        text, path = self._render(title, body, footer, tone, plain)
        if path:
            res = self._image_result(event, path)
            if res is not None:
                return res
        return event.plain_result(text)

    def _image_result(self, event, path):
        """按兼容性依次尝试各种发图方式，都不行返回 None"""
        # ① 最直接
        try:
            if hasattr(event, "image_result"):
                return event.image_result(path)
        except Exception as e:
            logger.warning(f"[kook_bridge] image_result 失败：{e}")
        # ② 消息链 fromFileSystem
        try:
            from astrbot.api.message_components import Image

            return event.chain_result([Image.fromFileSystem(path)])
        except Exception as e:
            logger.warning(f"[kook_bridge] Image.fromFileSystem 失败：{e}")
        # ③ base64（Windows 下 file:// 兼容性更好）
        try:
            import base64

            from astrbot.api.message_components import Image

            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            return event.chain_result([Image.fromBase64(b64)])
        except Exception as e:
            logger.warning(f"[kook_bridge] Image.fromBase64 失败：{e}")
        return None

    async def initialize(self):
        """插件加载后自动调用，日志里看到这行才说明插件真的被 AstrBot 加载了"""
        logger.info("=" * 50)
        logger.info("✅ [kook_bridge] 插件已加载")
        logger.info(f"   bridge_url     = {self.base_url}")
        logger.info(f"   bridge_token   = {'已设置(长度 %d)' % len(self.token) if self.token else '（空）'}")
        logger.info(f"   kook_channel_id= {self.channel_id or '（自动查找）'}")
        logger.info(f"   default_user   = {self.default_kook_user or '（未设置）'}")
        logger.info(f"   async_mode     = {self.async_mode}")
        logger.info(
            f"   private_only   = {self.private_only}"
            + ("  ← 仅私聊生效，群消息会被忽略" if self.private_only else "  ← 群聊与私聊都生效")
        )
        logger.info(f"   AstrBot 唤醒前缀 = {self.wake_prefix!r}")
        logger.info(f"   → QQ 里请发送：{self.wake_prefix}kook状态")
        logger.info("=" * 50)

    # ---------------- 绑定数据持久化 ----------------
    def _data_dir(self) -> str:
        try:
            if _HAS_STAR_TOOLS:
                d = os.path.join(str(StarTools.get_data_dir()), "kook_bridge")
            else:
                d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        except Exception:
            d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        os.makedirs(d, exist_ok=True)
        return d

    def _bind_path(self) -> str:
        return os.path.join(self._data_dir(), "bindings.json")

    def _load_bindings(self) -> dict:
        p = self._bind_path()
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"[kook_bridge] 绑定文件读取失败：{e}")
        return {}

    def _save_bindings(self):
        try:
            with open(self._bind_path(), "w", encoding="utf-8") as f:
                json.dump(self.bindings, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[kook_bridge] 绑定文件保存失败：{e}")

    # ---------------- HTTP ----------------
    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    async def _request(self, method: str, path: str, payload: dict | None = None):
        url = self.base_url + path
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            if method == "GET":
                async with sess.get(url, headers=self._headers()) as resp:
                    data = await resp.json(content_type=None)
                    if resp.status == 401:
                        data["error"] = self._auth_error_msg(data)
                    return data
            async with sess.post(url, headers=self._headers(), data=json.dumps(payload or {})) as resp:
                data = await resp.json(content_type=None)
                if resp.status == 401:
                    data["error"] = self._auth_error_msg(data)
                return data

    def _auth_error_msg(self, data: dict) -> str:
        """401 时给出可操作的中文提示"""
        mine = (self.token or "").strip()
        mine_desc = f"{mine[:2]}…{mine[-2:]}（长度 {len(mine)}）" if mine else "（空）"
        return (
            "🔒 Token 校验失败：KOOK 机器人与插件的 token 不一致\n"
            f"　插件这边：{mine_desc}\n"
            f"　KOOK 那边：{data.get('expected') or '未知'}\n"
            "　收到的是：" + str(data.get("received") or "未知") + "\n"
            "修复二选一：\n"
            "　① 两边填完全一样的字符串（注意别多空格、别带引号）\n"
            "　② 同机部署图省事：KOOK 的 .env 里 BRIDGE_TOKEN= 留空，插件配置里 bridge_token 也留空，重启两边\n"
            "改完记得：KOOK 机器人 npm start 重启 + AstrBot 重载插件"
        )

    @staticmethod
    def _is_user_id(s: str) -> bool:
        return bool(re.fullmatch(r"\d{5,}", s.strip()))

    # ---------------- QQ 端卡片边框排版 ----------------
    @staticmethod
    def _ch_width(ch: str) -> int:
        """字符显示宽度：中文/全角/emoji 算 2，其余算 1"""
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            return 2
        # emoji 区（含部分被标为 Neutral 的符号）在中文环境同样占两格
        cp = ord(ch)
        if (
            0x1F300 <= cp <= 0x1FAFF
            or 0x2600 <= cp <= 0x27BF
            or 0xFE00 <= cp <= 0xFE0F
            or 0x2B00 <= cp <= 0x2BFF
            or 0x1F1E6 <= cp <= 0x1F1FF
        ):
            return 2
        return 1

    @classmethod
    def _dw(cls, s: str) -> int:
        return sum(cls._ch_width(c) for c in s)

    @classmethod
    def _wrap(cls, s: str, max_w: int):
        """按显示宽度折行（尽量不在英文单词中间切断）"""
        out = []
        cur, cur_w = "", 0
        i = 0
        while i < len(s):
            ch = s[i]
            cw = cls._ch_width(ch)
            if cur_w + cw > max_w:
                out.append(cur.rstrip())
                cur, cur_w = "", 0
                continue
            cur += ch
            cur_w += cw
            i += 1
        if cur.strip():
            out.append(cur.rstrip())
        return out or [""]

    @classmethod
    def _card(cls, title: str, body: str = "", footer: str = None, max_w: int = 44) -> str:
        """渲染成卡片边框样式（QQ 端统一用它）

        ┌──────────────────────┐
        │  ✅ 标题              │
        ├──────────────────────┤
        │  正文                │
        ├──────────────────────┤
        │  页脚                │
        └──────────────────────┘
        """
        body_lines = []
        for raw in str(body or "").split("\n"):
            raw = raw.rstrip()
            if not raw.strip():
                body_lines.append("")
                continue
            body_lines.extend(cls._wrap(raw, max_w))

        foot_lines = []
        if footer:
            for raw in str(footer).split("\n"):
                raw = raw.rstrip()
                if not raw.strip():
                    continue
                foot_lines.extend(cls._wrap(raw, max_w))

        # 内容太长就不套边框，避免刷屏
        if len(body_lines) > 26:
            parts = [f"◆ {title}"]
            if body:
                parts.append(body.rstrip())
            if foot_lines:
                parts.append("─" * 12)
                parts.extend(foot_lines)
            return "\n".join(parts)

        candidates = [title] + body_lines + foot_lines
        inner = max((cls._dw(l) for l in candidates), default=10)
        inner = max(10, min(inner, max_w))

        def row(text: str) -> str:
            pad = inner - cls._dw(text)
            return "│ " + text + " " * max(0, pad) + " │"

        top = "┌" + "─" * (inner + 2) + "┐"
        mid = "├" + "─" * (inner + 2) + "┤"
        bottom = "└" + "─" * (inner + 2) + "┘"

        lines = [top, row(title)]
        if body_lines:
            lines.append(mid)
            lines.extend(row(l) for l in body_lines)
        if foot_lines:
            lines.append(mid)
            lines.extend(row(l) for l in foot_lines)
        lines.append(bottom)
        return "\n".join(lines)

    @staticmethod
    def _strip_cmd(text: str, names) -> str:
        """剥掉开头的指令名。

        不同 AstrBot 版本行为不一致：有的会把指令名从 message_str 里去掉，
        有的会原样保留（那样 message_str 就是「kook绑定 唛头#1367」）。
        这里做幂等处理：只有首词确实等于指令名时才剥，两种版本都能正常工作。
        """
        t = (text or "").strip()
        if not t:
            return ""
        # 去掉可能残留的唤醒前缀（/ ! . 。等）
        t = t.lstrip("/!.。!！、,， ")
        parts = t.split(None, 1)
        first = parts[0]
        low = first.lower()
        for n in names or ():
            if low == str(n).lower():
                rest = parts[1] if len(parts) > 1 else ""
                # 剥完为空说明用户只发了指令名，保留原样让上层走「用法提示」
                return rest.strip() or t
        return t

    @staticmethod
    def _res(title, body="", footer=None, tone="info", plain=None):
        """统一的回复结构：交给 _emit / _send_back 决定出图还是出文本

        body  保留原始 KMarkdown（**加粗** / > 引用 / `代码` / emoji），供图片渲染
        plain 清理后的纯文本，供图片不可用时的文本兜底
        """
        return {"title": title, "body": body or "", "footer": footer,
                "tone": tone, "plain": plain}

    # -------- KMarkdown → 纯文本（图片不可用时的兜底）--------
    _KM_MET = re.compile(r"\(met\)\d+\(met\)")
    _KM_FONT = re.compile(r"\(font\)(.*?)\(font\)\[[^\]]*\]", re.S)
    _KM_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
    _KM_CODE = re.compile(r"`([^`]+)`")
    _KM_ITALIC = re.compile(r"\*([^*\n]+?)\*")
    _KM_QUOTE = re.compile(r"^\s*>\s?", re.M)

    @classmethod
    def _to_plain(cls, s: str) -> str:
        s = cls._KM_MET.sub("", str(s or ""))
        s = cls._KM_FONT.sub(lambda m: m.group(1), s)
        s = s.replace("(font)", "")
        s = cls._KM_BOLD.sub(r"\1", s)
        s = cls._KM_CODE.sub(r"\1", s)
        s = cls._KM_ITALIC.sub(r"\1", s)
        s = cls._KM_QUOTE.sub("", s)
        return s.strip()

    # ---------------- 绑定（以 KOOK 端 info.json 为唯一数据源） ----------------
    async def _bind_request(self, qq: str, identifier: str) -> dict:
        payload = {"qq": qq, "identifier": identifier}
        if self.guild_id:
            payload["guild_id"] = self.guild_id
        return await self._request("POST", "/bind-request", payload)

    async def _bind_status(self, qq: str) -> dict:
        payload = {"qq": qq}
        if self.guild_id:
            payload["guild_id"] = self.guild_id
        return await self._request("POST", "/bind-status", payload)

    async def _bind_resolve(self, qq: str) -> dict:
        return await self._request("POST", "/bind-resolve", {"qq": qq})

    async def _bind_unbind(self, qq: str) -> dict:
        return await self._request("POST", "/bind-unbind", {"qq": qq})

    async def _bind_list(self) -> dict:
        return await self._request("POST", "/bind-list", {})

    async def _execute(self, command: str, qq: str) -> str:
        # 以 KOOK 端 info.json 为准；本地缓存仅作离线兜底
        kook_user = self.bindings.get(qq) or self.default_kook_user
        payload = {"command": command, "echo_to_kook": self.echo_to_kook, "qq": qq}
        if self.guild_id:
            payload["guild_id"] = self.guild_id
        if self.channel_id:
            payload["channel_id"] = self.channel_id
        if kook_user:
            if self._is_user_id(kook_user):
                payload["kook_user_id"] = kook_user
            else:
                payload["kook_user"] = kook_user

        data = await self._request("POST", "/command", payload)
        if not isinstance(data, dict):
            return self._res("执行失败", "KOOK 机器人返回了无法解析的内容", tone="error")

        dbg = data.get("debug") or {}
        # 页脚：始终标明「谁在用什么身份执行」，便于群里追溯
        who = dbg.get("kook_user") or "未知"
        admin_txt = "管理员" if dbg.get("is_admin") else "非管理员"
        chan = dbg.get("channel_name") or "未知"
        footer = f"操作者　{who}　·　{admin_txt}\n落地频道　{chan}"

        if not data.get("ok"):
            body = data.get("error") or "KOOK 机器人执行失败"
            if data.get("detail"):
                body += f"\n{data['detail']}"
            fix = data.get("fix")
            return self._res("指令未执行", body, f"💡 {fix}" if fix else None, tone="error")

        replies = [r.strip() for r in (data.get("replies") or []) if r and r.strip()]
        if not replies:
            lines = [
                "指令已送达，但 KOOK 机器人没有任何回复。",
                f"落地频道　{chan}",
                f"指令识别　{dbg.get('matched_command') or '未知'}",
                f"执行身份　{who}（{admin_txt}）",
            ]
            if not dbg.get("is_admin"):
                lines.append("该账号不是管理员，管理类指令会被直接拦下。")
            return self._res(
                "无回复", "\n".join(lines),
                "发「kook身份」核对绑定 · 发「kook菜单」核对写法", tone="warning"
            )

        matched = dbg.get("matched_command") or "执行完成"
        plain_list = [r.strip() for r in (data.get("replies_plain") or []) if r and r.strip()]
        return self._res(
            matched, "\n\n".join(replies), footer, tone="success",
            plain="\n\n".join(plain_list) or None,
        )

    @staticmethod
    def _strip_self(text: str) -> str:
        """去掉可能残留的「kook」前缀。

        不同版本 AstrBot 的 message_str 有时不含命令名、有时含，
        这里两种都兼容，避免把「kook 要塞 小明#1234」整串当指令发给 KOOK。
        """
        t = (text or "").strip()
        # 全角空格也当成分隔符
        t = t.replace("\u3000", " ")
        # 形如「kook xxx」「kook　xxx」「kookxxx」→ 去掉开头的 kook
        m = re.match(r"^\s*kook\s*(.*)$", t, re.IGNORECASE)
        if m:
            return m.group(1).strip()
        m2 = re.match(r"^\s*kook(.*)$", t, re.IGNORECASE)
        if m2:
            return m2.group(1).strip()
        return t

    # ---------------- 会话类型判定 ----------------
    def _is_private_chat(self, event: AstrMessageEvent) -> bool:
        """判断当前消息是否来自私聊（好友单聊）

        ⚠️ 不能只靠「群号为空」判断：
           频道消息（GuildMessage）在某些适配器下群号也是空的，
           会被误判成私聊。所以先看【显式消息类型】，再看群号。

        判定优先级：
        ① unified_msg_origin 的消息类型段（FriendMessage / GroupMessage / GuildMessage）
        ② event.message_obj.type（有适配器直接给了枚举）
        ③ 群号是否为空
        ④ event.is_private_chat()（框架原生 API，兜底）
        """
        # ① unified_msg_origin：platform:MessageType:session_id
        try:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            parts = umo.split(":")
            if len(parts) >= 2:
                t = parts[1].lower()
                if "friend" in t or "private" in t:
                    return True
                # guild / channel 也算「非私聊」，否则会被群号为空误判成私聊
                if "group" in t or "guild" in t or "channel" in t:
                    return False
        except Exception:
            pass

        # ② 消息对象上的类型枚举
        try:
            mt = getattr(getattr(event, "message_obj", None), "type", None)
            if mt is not None:
                name = str(getattr(mt, "value", mt)).lower()
                if "friend" in name or "private" in name:
                    return True
                if "group" in name or "guild" in name or "channel" in name:
                    return False
        except Exception:
            pass

        # ③ 群号：非空 → 一定是群/频道
        gid = None
        try:
            fn = getattr(event, "get_group_id", None)
            if callable(fn):
                gid = fn()
        except Exception:
            pass
        if gid is None:
            try:
                gid = getattr(getattr(event, "message_obj", None), "group_id", None)
            except Exception:
                gid = None
        if gid is not None:
            return not str(gid).strip()

        # ④ 框架原生 API 兜底
        try:
            fn = getattr(event, "is_private_chat", None)
            if callable(fn):
                return bool(fn())
        except Exception:
            pass

        # 全都判断不了时按「非私聊」处理（保守，避免误放行群消息）
        return False

    def _chat_desc(self, event: AstrMessageEvent) -> str:
        """给诊断信息用的会话描述，如「私聊」「群聊（123456）」"""
        private = self._is_private_chat(event)
        if private:
            return "私聊"
        gid = ""
        try:
            gid = str(event.get_group_id() or "")
        except Exception:
            pass
        if not gid:
            try:
                gid = str(getattr(getattr(event, "message_obj", None), "group_id", "") or "")
            except Exception:
                pass
        return f"群聊（{gid}）" if gid else "群聊"

    def _scope_ok(self, event: AstrMessageEvent) -> bool:
        """作用域检查：开了「仅私聊」时，群消息一律不处理

        群里被拦下时【静默返回】，不回复任何内容 —— 避免在群里刷屏。
        """
        if not self.private_only:
            return True
        if self._is_private_chat(event):
            return True
        logger.info(
            f"[kook_bridge] 已开启「仅私聊生效」，忽略来自 {self._chat_desc(event)} 的消息"
        )
        return False

    # ---------------- 权限 ----------------
    def _allowed(self, event: AstrMessageEvent) -> bool:
        qq = str(event.get_sender_id())
        if qq in self.allow_qq:
            return True
        if self.admin_only:
            try:
                admins = {str(a) for a in (self.context.get_config().get("admins_id") or [])}
            except Exception:
                admins = set()
            return qq in admins
        return True

    # ---------------- 指令 ----------------
    @filter.command("kook")
    async def kook_cmd(self, event: AstrMessageEvent):
        """转发指令给 KOOK 机器人，例：kook 要塞 小明#1234"""
        if not self._scope_ok(event):
            return

        text = self._strip_self((event.message_str or ""))
        if not text:
            yield self._emit(
                event, "KOOK 指令桥",
                "用法　kook <指令>\n\n"
                "　kook 要塞 小明#1234\n"
                "　kook 分组列表\n"
                "　kook 联盟 1厅 要塞 小明#1234",
                "首次使用请先：绑定 你的KOOK用户名#ID", tone="info",
            )
            return
        if not self._allowed(event):
            yield self._emit(event, "无权限", "你没有使用此功能的权限", tone="error")
            return

        qq = str(event.get_sender_id())
        if self.async_mode:
            # ⚠️ 必须保留 task 的强引用：
            #    asyncio 官方文档明确警告「Save a reference to the result of create_task,
            #    to avoid a task disappearing mid-execution」。
            #    如果不保存，任务可能在跑完前被 GC 回收 —— 表现为「已转发」后再无任何回复。
            task = asyncio.create_task(self._run_and_report(event, text, qq))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            yield self._emit(event, "已转发", text[:80], "执行完成后自动回传结果", tone="info")
        else:
            try:
                result = await self._execute(text, qq)
            except Exception as e:
                result = self._res("调用失败", str(e), tone="error")
            yield self._emit(event, **result)

    async def _run_and_report(self, event: AstrMessageEvent, text: str, qq: str):
        try:
            res = await self._execute(text, qq)
        except aiohttp.ClientError as e:
            res = self._res("连不上 KOOK 机器人", f"{self.base_url}\n{e}", "确认机器人窗口开着且已 npm start", tone="error")
        except asyncio.TimeoutError:
            res = self._res("响应超时", "指令可能仍在执行中", "请到 KOOK 频道确认结果", tone="warning")
        except Exception as e:
            res = self._res("执行失败", str(e), tone="error")
        await self._send_back(event, **res)

    async def _send_back(self, event: AstrMessageEvent, title=None, body="", footer=None, tone="info", msg=None, plain=None):
        """异步回传结果（优先图片，失败降级文本）。

        不同 AstrBot 版本可用的主动发送 API 不一样，这里逐个尝试，
        并在全部失败时把错误打进日志（避免「只看到已转发、再没下文」的假象）。
        """
        if msg is not None:  # 兼容旧的纯字符串调用
            title = None
        elif title is not None:
            text, path = self._render(title, body, footer, tone, plain)
            if path:
                # 方式 0：直接发图
                try:
                    if hasattr(event, "send"):
                        await event.send(event.image_result(path))
                        return
                except Exception as e:
                    logger.warning(f"[kook_bridge] 异步发图失败，尝试消息链：{e}")
                res = self._image_result(event, path)
                if res is not None:
                    try:
                        if hasattr(event, "send"):
                            await event.send(res)
                            return
                    except Exception:
                        pass
            msg = text
        else:
            msg = body

        # 方式 1：event.send（新版本推荐）
        try:
            if hasattr(event, "send"):
                await event.send(event.plain_result(msg))
                return
        except Exception as e:
            logger.warning(f"[kook_bridge] event.send 失败，尝试其他方式：{e}")

        # 方式 2：context.send_message
        try:
            umo = getattr(event, "unified_msg_origin", None)
            if umo and hasattr(self.context, "send_message"):
                await self.context.send_message(umo, event.plain_result(msg))
                return
        except Exception as e:
            logger.warning(f"[kook_bridge] context.send_message 失败：{e}")

        logger.error(
            f"[kook_bridge] ❌ 结果回传失败（两种 API 都不可用）。内容如下：\n{msg}\n"
            "   → 建议到插件配置里把 async_mode 关掉，改同步模式即可正常收到回复"
        )

    @filter.command("kook调试", alias={"kookdebug", "kookdbg"})
    async def kook_debug(self, event: AstrMessageEvent):
        """同步执行一次指令并回传【原始返回】，用于定位「为什么只有菜单能用」

        例：kook调试 要塞 小明#1234
        """
        raw = (event.message_str or "").strip()
        if not raw:
            yield event.plain_result(
                "用法：kook调试 <和 kook 一样的指令>\n"
                "例：kook调试 分组列表\n"
                "　　kook调试 要塞 小明#1234\n\n"
                "本命令【同步】执行，会把 KOOK 机器人返回的原始内容整段贴回来"
            )
            return

        qq = str(event.get_sender_id())
        payload = {
            "command": raw,
            "echo_to_kook": self.echo_to_kook,
        }
        if self.guild_id:
            payload["guild_id"] = self.guild_id
        if self.channel_id:
            payload["channel_id"] = self.channel_id
        kook_user = self.bindings.get(qq) or self.default_kook_user
        if kook_user:
            if self._is_user_id(kook_user):
                payload["kook_user_id"] = kook_user
            else:
                payload["kook_user"] = kook_user

        try:
            data = await self._request("POST", "/command", payload)
        except Exception as e:
            yield event.plain_result(f"❌ 请求失败：{type(e).__name__}: {e}")
            return

        txt = json.dumps(data, ensure_ascii=False, indent=2)
        if len(txt) > 1500:
            txt = txt[:1500] + "\n…（已截断）"
        head = [
            "🔍 调试结果（同步执行）",
            f"发送指令：{raw!r}",
            f"插件配置：channel_id={self.channel_id or '（自动）'} guild_id={self.guild_id or '（自动）'}",
            f"执行身份：{kook_user or '（未绑定）'}",
            "─" * 24,
        ]
        yield event.plain_result("\n".join(head) + "\n" + txt)

    @filter.command("绑定", alias={"bind", "kook验证绑定", "绑定kook", "绑定KOOK"})
    async def bind_verify(self, event: AstrMessageEvent):
        """发起绑定验证：绑定 唛头#1367 → KOOK 私聊本人确认后才生效"""
        arg = self._strip_cmd(
            event.message_str,
            ["绑定", "bind", "kook绑定", "kookbind", "kookbind2", "kook验证绑定", "绑定kook"],
        )
        async for r in self._do_bind(event, arg):
            yield r

    @filter.command("kook绑定", alias={"kookbind", "kookbind2"})
    async def kook_bind(self, event: AstrMessageEvent):
        """「kook绑定」与「绑定」等价，统一走私聊验证流程"""
        arg = self._strip_cmd(event.message_str, ["kook绑定", "kookbind", "kookbind2"])
        async for r in self._do_bind(event, arg):
            yield r

    async def _legacy_bind(self, event: AstrMessageEvent):
        """（保留备用）不经私聊验证的直接绑定"""
        arg = self._strip_cmd(event.message_str, ["kook绑定", "kookbind", "kookbind2"])
        if not arg:
            yield event.plain_result(
                "用法：kook绑定 <KOOK用户名#ID> 或 kook绑定 <KOOK用户ID>\n例：kook绑定 唛头#1367"
            )
            return
        payload = {"identifier": arg}
        if self.guild_id:
            payload["guild_id"] = self.guild_id
        try:
            data = await self._request("POST", "/resolve-user", payload)
        except Exception as e:
            yield event.plain_result(f"❌ 连接 KOOK 机器人失败：{e}")
            return
        if not data.get("ok"):
            err = data.get("error") or "未找到该用户"
            lines = [f"❌ 绑定失败：{err}"]
            cand = data.get("candidates") or []
            if cand:
                lines.append("💡 服务器里叫类似名字的成员：")
                for c in cand[:8]:
                    tag = f"{c.get('username') or ''}#{c.get('identify_num') or ''}".strip("#")
                    lines.append(f"　· {tag}（ID {c.get('user_id')}）")
            lines.append(
                "💡 注意：用户名和 # 后面是 KOOK 的四位编号（不是 QQ 号、也不是用户 ID）。\n"
                "　在 KOOK 里点自己头像可看到，例：唛头#1367"
            )
            yield event.plain_result("\n".join(lines))
            return

        qq = str(event.get_sender_id())
        self.bindings[qq] = str(data.get("user_id"))
        self._save_bindings()
        name = data.get("username") or ""
        disc = data.get("identify_num") or ""
        tag = f"{name}#{disc}" if name and disc else str(data.get("user_id"))
        yield event.plain_result(f"✅ 已绑定 QQ {qq} → KOOK「{tag}」\n之后直接发「kook <指令>」即可，权限按该 KOOK 账号的身份组判定")

    async def _do_bind(self, event: AstrMessageEvent, arg: str):
        """绑定主流程：发私聊 → 等本人确认 → 写入 KOOK 端 info.json"""
        if not self._scope_ok(event):
            return

        if not arg:
            yield self._emit(
                event, "绑定用法",
                "用法　绑定 <KOOK用户名#ID>\n例　　绑定 唛头#1367\n\n"
                "流程　机器人私聊该 KOOK 账号\n　　→ 本人回复「确定绑定」\n　　→ 绑定生效",
                "该账号必须先进入机器人所在的 KOOK 服务器", tone="info",
            )
            return

        qq = str(event.get_sender_id())
        try:
            r = await self._bind_request(qq, arg)
        except Exception as e:
            yield self._emit(event, "连接失败", str(e), "确认 KOOK 机器人正在运行", tone="error")
            return

        if not r.get("ok"):
            lines = [r.get("error") or "绑定失败"]
            for c in (r.get("candidates") or [])[:8]:
                tag = f"{c.get('username') or ''}#{c.get('identify_num') or ''}".strip("#")
                lines.append(f"· {tag}")
            footer = r.get("fix") or "# 后是 KOOK 四位编号，不是 QQ 号"
            yield self._emit(event, "无法发起绑定", "\n".join(lines), footer, tone="error")
            return

        if r.get("verified"):
            b = r.get("binding") or {}
            self.bindings[qq] = str(b.get("kook_id"))
            self._save_bindings()
            tag = b.get("kook_tag") or b.get("kook_id")
            yield self._emit(
                event, "绑定成功", f"QQ {qq}　↔　KOOK「{tag}」",
                "未启用私聊验证" if r.get("skipped_verify") else "现在可以发「kook <指令>」了",
                tone="success",
            )
            return

        # 已发出私聊，后台轮询等待本人确认
        wait_sec = int(r.get("expires_in") or 180)
        yield self._emit(
            event, "验证已发送",
            f"已私聊 KOOK「{r.get('kook_tag')}」\n"
            f"请在 {wait_sec // 60} 分钟内到 KOOK 回复\n\n"
            f"　确定绑定　→ 同意\n　取消　　　→ 拒绝",
            "等待确认中…（超时自动失效）", tone="warning",
        )
        task = asyncio.create_task(self._wait_bind_confirm(event, qq, wait_sec))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _wait_bind_confirm(self, event: AstrMessageEvent, qq: str, wait_sec: int):
        """轮询等待 KOOK 私聊确认，最多等 wait_sec 秒"""
        deadline = asyncio.get_event_loop().time() + wait_sec
        interval = 4
        last_notice = 0
        try:
            while True:
                await asyncio.sleep(interval)
                st = await self._bind_status(qq)
                status = st.get("status")
                if status == "bound":
                    b = st.get("binding") or {}
                    self.bindings[qq] = str(b.get("kook_id"))
                    self._save_bindings()
                    tag = b.get("kook_tag") or b.get("kook_id")
                    await self._send_back(
                        event, title="绑定成功", body=f"QQ {qq}　↔　KOOK「{tag}」",
                        footer="现在可以发「kook <指令>」了", tone="success",
                    )
                    return
                if status in ("cancelled",):
                    await self._send_back(
                        event, title="绑定被拒绝", body="对方在 KOOK 里回复了「取消」", tone="error"
                    )
                    return
                if status == "expired":
                    await self._send_back(
                        event, title="验证超时", body="未收到「确定绑定」回复",
                        footer="重新发送「绑定 用户名#ID」", tone="warning",
                    )
                    return
                # 还剩下很久时提醒一次
                now = asyncio.get_event_loop().time()
                remain = deadline - now
                if remain <= 0:
                    diag = st.get("diag")
                    await self._send_back(
                        event, title="验证超时", body="未收到「确定绑定」回复",
                        footer=diag or "重新发送「绑定 用户名#ID」", tone="warning",
                    )
                    return
                if last_notice == 0 and remain < wait_sec / 2:
                    last_notice = 1
                    await self._send_back(
                        event, title="等待确认", body=f"还剩约 {int(remain)} 秒",
                        footer="请尽快到 KOOK 私聊回复「确定绑定」", tone="warning",
                    )
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"[kook_bridge] 等待绑定确认异常：{e}")
            try:
                await self._send_back(event, title="等待确认异常", body=str(e), tone="error")
            except Exception:
                pass

    @filter.command("解绑", alias={"unbind", "kook解绑"})
    async def bind_unbind(self, event: AstrMessageEvent):
        """解绑当前 QQ 的 KOOK 身份"""
        if not self._scope_ok(event):
            return

        qq = str(event.get_sender_id())
        try:
            r = await self._bind_unbind(qq)
        except Exception as e:
            yield self._emit(event, "解绑失败", str(e), tone="error")
            return
        self.bindings.pop(qq, None)
        self._save_bindings()
        if r.get("removed"):
            yield self._emit(event, "已解绑", f"QQ {qq} 的 KOOK 身份已解除", tone="success")
        else:
            yield self._emit(event, "无需解绑", "当前 QQ 没有绑定记录", tone="info")

    @filter.command("绑定列表", alias={"bindlist", "kook绑定列表"})
    async def bind_list(self, event: AstrMessageEvent):
        """查看所有 QQ ↔ KOOK 绑定关系"""
        if not self._scope_ok(event):
            return

        if not self._allowed(event):
            yield self._emit(event, "无权限", "你没有使用此功能的权限", tone="error")
            return
        try:
            r = await self._bind_list()
        except Exception as e:
            yield self._emit(event, "查询失败", str(e), tone="error")
            return
        items = r.get("bindings") or []
        if not items:
            yield self._emit(event, "绑定列表", "暂无绑定记录", tone="info")
            return
        lines = []
        for b in items[:50]:
            tag = b.get("kook_tag") or b.get("kook_id")
            when = str(b.get("bound_at") or "")[:10]
            lines.append(f"QQ {b.get('qq')}　↔　{tag}" + (f"　({when})" if when else ""))
        yield self._emit(event, f"绑定列表 · {len(items)} 条", "\n".join(lines), tone="info")

    @filter.command("kook解绑", alias={"kookunbind"})
    async def kook_unbind(self, event: AstrMessageEvent):
        """解绑当前 QQ 对应的 KOOK 身份"""
        if not self._scope_ok(event):
            return

        qq = str(event.get_sender_id())
        if qq in self.bindings:
            self.bindings.pop(qq)
            self._save_bindings()
            yield self._emit(event, "已解绑", "KOOK 身份已解除", tone="success")
        else:
            yield self._emit(event, "无需解绑", "当前 QQ 尚未绑定 KOOK 身份", tone="info")

    @filter.command("kook身份", alias={"kookwhoami", "kookwho"})
    async def kook_whoami(self, event: AstrMessageEvent):
        """查看当前 QQ 绑定的 KOOK 身份与管理员权限"""
        if not self._scope_ok(event):
            return

        qq = str(event.get_sender_id())
        # 以 KOOK 端 info.json 为准
        bound_id = None
        try:
            rb = await self._bind_resolve(qq)
            if rb.get("bound"):
                bound_id = str(rb.get("kook_id"))
        except Exception:
            pass
        bound = bound_id or self.bindings.get(qq)
        eff = bound or self.default_kook_user
        if not eff:
            yield self._emit(
                event, "尚未绑定", "请先发「绑定 你的KOOK用户名#ID」",
                "会私聊发一条验证消息，回复「确定绑定」后生效", tone="warning",
            )
            return
        payload = {"qq": qq}
        if self.guild_id:
            payload["guild_id"] = self.guild_id
        if bound and self._is_user_id(bound):
            payload["kook_user_id"] = bound
        else:
            payload["kook_user"] = eff
        try:
            d = await self._request("POST", "/whoami", payload)
        except Exception as e:
            yield self._emit(event, "查询失败", f"绑定 ID：{bound or eff}\n{e}", tone="error")
            return
        if not d.get("ok"):
            yield self._emit(event, "查询失败", f"绑定：{eff}\n{d.get('error')}", tone="error")
            return
        roles = ", ".join(str(x) for x in (d.get("user_roles") or [])) or "无"
        adm_roles = ", ".join(str(x) for x in (d.get("admin_role_ids") or [])) or "无"
        is_admin = bool(d.get("is_admin"))
        lines = [
            f"KOOK 用户　　{d.get('kook_user_name') or '未知'}（{d.get('kook_user_id')}）",
            f"服务器　　　{d.get('guild_id')}",
            f"我的身份组　{roles}",
            f"管理员组　　{', '.join(d.get('admin_role_names') or [])} → {adm_roles}",
        ]
        title = "身份 · 管理员" if is_admin else "身份 · 非管理员"
        footer = None
        if not is_admin:
            footer = "除「菜单」外，其余指令都要求管理员权限"
        for p in d.get("problems") or []:
            lines.append(p)
        yield self._emit(
            event, title, "\n".join(lines), footer, tone="success" if is_admin else "error"
        )

    @filter.command("kook状态", alias={"kookstatus", "kookping", "kkstatus"})
    async def kook_status(self, event: AstrMessageEvent):
        """检查与 KOOK 机器人桥接是否连通"""
        if not self._scope_ok(event):
            return

        try:
            data = await self._request("GET", "/health")
        except Exception as e:
            yield self._emit(
                event, "桥接不通", f"无法连接 KOOK 机器人\n{self.base_url}\n{e}",
                "确认机器人窗口开着，且已 npm start 重启", tone="error",
            )
            return
        if not data.get("ok"):
            yield self._emit(event, "桥接异常", str(data)[:400], tone="warning")
            return

        auth = "已启用" if data.get("auth_required") else "未启用"
        mine = self.token
        mine_desc = f"{mine[:2]}…{mine[-2:]}（{len(mine)}）" if mine else "（空）"
        lines = [
            f"机器人　　　{data.get('bot') or '未知'}",
            f"服务器　　　{data.get('guild') or '未知'}",
            f"落地频道　　{data.get('channel') or '未知'}",
            f"允许频道　　{'、'.join(data.get('channels') or []) or '未知'}",
            f"鉴权　　　　{auth}",
            f"　KOOK 端 token　{data.get('token') or '（空）'}",
            f"　插件端 token　{mine_desc}",
            f"生效范围　　　{'仅私聊（群消息已忽略）' if self.private_only else '群聊 + 私聊'}",
            f"当前会话　　　{self._chat_desc(event)}",
        ]
        footer = None
        if data.get("guild_corrected"):
            footer = (
                f"配置的服务器 ID {data.get('guild_configured')} 无效，"
                f"已自动改用 {data.get('guild')}，建议同步修改配置"
            )
        yield self._emit(event, "桥接正常", "\n".join(lines), footer, tone="success")

    @filter.command("kook菜单", alias={"kookmenu"})
    async def kook_menu(self, event: AstrMessageEvent):
        """拉取 KOOK 机器人的功能菜单"""
        if not self._scope_ok(event):
            return

        if not self._allowed(event):
            yield self._emit(event, "无权限", "你没有使用此功能的权限", tone="error")
            return
        try:
            result = await self._execute("菜单", str(event.get_sender_id()))
        except Exception as e:
            result = self._res("调用失败", str(e), tone="error")
        yield self._emit(event, **result)

    @filter.command("kook服务器", alias={"kookguilds", "kookguild"})
    @filter.command("kook字体", alias={"kookfont", "kookfonts"})
    async def kook_fonts(self, event: AstrMessageEvent):
        """诊断图片渲染用的字体，排查 emoji 显示为方块/空白的问题"""
        if not self._scope_ok(event):
            return

        ci = self._get_card_image()
        if not ci:
            yield self._emit(
                event, "图片模块不可用",
                "card_image.py 加载失败（可能没装 Pillow）\n当前回复为文本卡片",
                "在 AstrBot 环境里执行 pip install Pillow", tone="error",
            )
            return

        cn = ci.find_font(self.font_path)
        em = ci.find_emoji_font(self.emoji_font_path)
        lines = [
            f"中文字体　{cn or '未找到'}",
            f"emoji 字体　{em or '未找到'}",
            f"图片输出目录　{self.image_dir}",
        ]
        if not cn:
            yield self._emit(
                event, "缺少中文字体", "\n".join(lines),
                "装 Pillow 后仍如此，可在插件配置 image_font_path 手动指定字体路径",
                tone="error",
            )
            return
        if not em:
            lines.append("")
            lines.append("未找到可用的 emoji 字体，图片里的 emoji 已被剥掉（不会显示方块）")
            yield self._emit(
                event, "字体状态", "\n".join(lines),
                "想显示彩色 emoji：在插件配置 image_emoji_font_path 填 C:/Windows/Fonts/seguiemj.ttf",
                tone="warning",
            )
            return

        lines.append("")
        lines.append("emoji 可正常渲染")
        yield self._emit(event, "字体状态", "\n".join(lines), tone="success")

    async def kook_guilds(self, event: AstrMessageEvent):
        """列出 KOOK 服务器与频道 ID，便于填写配置"""
        if not self._scope_ok(event):
            return

        if not self._allowed(event):
            yield self._emit(event, "无权限", "你没有使用此功能的权限", tone="error")
            return
        try:
            data = await self._request("POST", "/guilds", {"guild_id": self.guild_id} if self.guild_id else {})
        except Exception as e:
            yield self._emit(event, "查询失败", str(e), tone="error")
            return
        guilds = (data or {}).get("guilds") or []
        if not guilds:
            yield self._emit(event, "无服务器", str(data)[:300], tone="warning")
            return
        lines = []
        for g in guilds:
            lines.append(f"{g.get('name')}")
            lines.append(f"　服务器 ID　{g.get('id')}")
            for c in (g.get("channels") or [])[:20]:
                lines.append(f"　└ {c.get('name')}　{c.get('id')}")
        using = (data or {}).get("using") or {}
        footer = None
        if using.get("guild_id"):
            footer = f"当前使用　服务器 {using.get('guild_id')} · 频道 {using.get('channel_id') or '自动'}"
        yield self._emit(event, "服务器与频道", "\n".join(lines), footer, tone="info")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _kook_hint(self, event: AstrMessageEvent):
        """兜底提示：消息看起来像本插件指令却没被识别时，多半是没带唤醒前缀

        注意：正常命令被 @filter.command 处理后事件会终止，不会走到这里，因此不会重复回复。
        """
        if not self._scope_ok(event):
            return

        text = (event.message_str or "").strip()
        if not text.lower().startswith("kook"):
            return
        # 能被 command 正常匹配的（说明带对了前缀）就不打扰
        known = {
            "kook", "kook绑定", "kookbind", "kook解绑", "kookunbind",
            "kook身份", "kookwhoami", "kookwho", "kook状态", "kookstatus",
            "kookping", "kkstatus", "kook菜单", "kookmenu", "kook服务器",
            "kookguilds", "kookguild",
        }
        head = text.split()[0] if text.split() else text
        if head in known:
            return
        yield self._emit(
            event, "漏了唤醒前缀", f"请改成　{self.wake_prefix}{text}",
            f"当前前缀 {self.wake_prefix!r}（可在 AstrBot 配置里改 wake_prefix）", tone="warning",
        )

    async def terminate(self):
        """插件卸载时清理"""
        for t in list(self._tasks):
            t.cancel()
        self._tasks.clear()
