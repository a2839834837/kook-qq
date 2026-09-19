"""
关键词自动回复插件
====================
功能：当群聊/私聊消息命中配置中的关键词时，自动回复对应内容。
     - 支持精确匹配、包含匹配、正则匹配三种模式
     - 支持设置回复前缀（@发送者 / 不@）
     - 触发记录持久化到插件 KV 存储（命中次数、最近命中时间）
     - 关键词与回复内容可在 WebUI 面板可视化配置
     - ★运行时动态管理规则（无需进 WebUI，机器人指令即可增删查）
        /reply-add <关键词> <回复内容> [模式]
        /reply-del <关键词>
        /reply-list
        /reply-stats
     - ★生效群聊（v1.4）：自动读取 bot 所在全部 QQ 群，可在 WebUI 面板
       下拉勾选「哪些群生效」，也可用指令增删
        /reply-groups             列出 bot 所在全部群聊（并标注是否生效）
        /reply-group-add [群号]    把群加入生效名单（不填群号 = 当前群）
        /reply-group-del <群号>    把群移出生效名单
        /reply-scope [模式]        查看/切换 all(全部) / whitelist(仅名单内) / blacklist(排除名单内)

使用：把整个文件夹放到 AstrBot 的 data/plugins/ 目录下，WebUI 重载插件即可。
"""

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger

import os
import re
import json
import asyncio
import threading

# ============================================================================
# 【重要】关于 AstrBotConfig 的导入
# ----------------------------------------------------------------------------
# AstrBotConfig 在 *新版本*（≥4.10+）中可通过 `from astrbot.api import AstrBotConfig` 获取；
# 但在 *旧版本*（如 4.9.x）中它并未从 astrbot.api.star 导出，直接 `import` 会报：
#   cannot import name 'AstrBotConfig' from 'astrbot.api.star'
# 由于 AstrBotConfig 本质就是个 dict 子类，插件里我们只用到 `.get()` 等字典方法，
# 因此这里仅在「类型检查」阶段才需要它，运行时完全不 import —— 兼容所有版本。
# 如果你的版本较新、想要更精确的类型提示，取消下面注释即可：
#   from astrbot.api import AstrBotConfig  # 新版
#   # 或：from astrbot.core.config.astrbot_config import AstrBotConfig  # 所有版本
# ============================================================================
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    try:
        from astrbot.api import AstrBotConfig
    except ImportError:
        from astrbot.core.config.astrbot_config import AstrBotConfig
else:
    AstrBotConfig = None  # type: ignore[assignment]


# ============================================================
# 常量
# ============================================================

# 规则存储用的 KV 键名（持久化在这里，不依赖 WebUI 是否保存）
KV_KEY_RULES = "keyreply:rules"

# 生效群聊名单的 KV 键名（当 WebUI 配置无法写回时的降级存储）
KV_KEY_GROUPS = "keyreply:groups"

# 群列表缓存文件名（bot 所在群聊的群号 + 群名，用于 WebUI 下拉展示）
LOCAL_GROUPS_FILE = "keyreply_groups.json"

# 生效群聊模式：all=全部群 / whitelist=仅名单内 / blacklist=排除名单内
GROUP_MODES = ("all", "whitelist", "blacklist")

# 群列表自动同步进 WebUI 面板前的等待秒数（等协议端连上，太快拉不到）
GROUP_SYNC_DELAY = 20

# 单次 /reply-groups 最多输出多少条（防止刷屏）
GROUP_LIST_MAX_SHOWN = 60

# 插件目录 / 配置文件路径（用于把群列表动态写进 _conf_schema.json）
PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEMA_FILE = os.path.join(PLUGIN_DIR, "_conf_schema.json")

# ★本地备份文件路径。规则的真源优先用插件 KV 存储（框架注入的 put_kv_data），
#   若你的环境 KV 不可用（部分旧版 / 特殊部署），会自动降级为本地 JSON 文件，
#   保证「添加条目」一定能持久化。
#   ${DATA_DIR} 会被自动替换为 AstrBot 数据目录；找不到则存插件目录下。
LOCAL_RULES_FILE = "keyreply_rules.json"

# 允许的规则匹配模式
VALID_MODES = ("contains", "exact", "regex")

# ★可修改点：允许哪些 QQ 号通过指令管理规则（留空 = 不限制，仅建议调试期使用）
ADMIN_IDS: list[str] = []


# ============================================================
# 本地文件存储（KV 不可用时的降级方案）
# ============================================================

def _get_data_dir() -> str:
    """尽量拿到 AstrBot 数据目录，失败则退回插件目录。"""
    for env_key in ("ASTRBOT_DATA_DIR", "DATA_DIR"):
        d = os.environ.get(env_key, "").strip()
        if d and os.path.isdir(d):
            return d
    # 常见相对路径回退
    for cand in ("../../data", "../data", "data"):
        if os.path.isdir(cand):
            return os.path.abspath(cand)
    return os.path.abspath(".")


class _LocalStore:
    """线程安全的本地 JSON 规则存储。"""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> list[dict] | None:
        if not os.path.exists(self.path):
            return None
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else None
        except Exception as e:
            logger.warning(f"[keyreply] 读取本地规则文件失败: {e}")
            return None

    def save(self, rules: list[dict]) -> bool:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with self._lock, open(self.path, "w", encoding="utf-8") as f:
                json.dump(rules, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            logger.warning(f"[keyreply] 写入本地规则文件失败: {e}")
            return False

# ============================================================
# 工具函数
# ============================================================

def _match(keyword: str, text: str, mode: str) -> bool:
    """根据匹配模式判断 text 是否命中 keyword。"""
    if mode == "exact":
        return keyword == text
    if mode == "regex":
        import re
        try:
            return re.search(keyword, text) is not None
        except re.error:
            logger.warning(f"[keyreply] 无效正则表达式: {keyword}")
            return False
    # 默认 "contains"：包含即命中
    return keyword in text


def _parse_add_args(text: str) -> tuple[str, str, str] | None:
    """
    解析 /reply-add 参数。支持两种写法：
      1) /reply-add 关键词 | 回复内容 [| 模式]
      2) /reply-add 关键词  回复内容            （以第一个空格分隔，不推荐含空格的关键词）
    返回 (keyword, reply, mode) 或 None（解析失败）。
    """
    body = text.strip()
    # 去掉命令名（兼容 /reply-add、/reply_add、/replyadd 三种写法）
    for prefix in ("/reply-add", "/reply_add", "/replyadd"):
        if body.startswith(prefix):
            body = body[len(prefix):]
            break
    body = body.strip()

    # 优先按 "|" 分隔（更稳妥，关键词/回复里可含空格）
    if "|" in body:
        parts = [p.strip() for p in body.split("|")]
    else:
        # 回退：以第一个空白分段
        parts = body.split(None, 2)

    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    keyword, reply = parts[0], parts[1]
    mode = (parts[2] if len(parts) >= 3 else "").strip() or "contains"
    return keyword, reply, mode


async def _onebot_call(client, action: str, **params):
    """
    调用 OneBot 协议端 API，兼容 aiocqhttp 的两种写法：
      client.api.call_action(action, **params)   # 常见
      client.call_action(action, **params)       # 部分版本
    成功返回响应 dict / list，失败返回 None（不抛异常，避免影响主流程）。
    """
    for obj in (getattr(client, "api", None), client):
        if obj is None:
            continue
        fn = getattr(obj, "call_action", None)
        if not callable(fn):
            continue
        try:
            ret = fn(action, **params)
            if hasattr(ret, "__await__"):  # 协程就等它
                ret = await ret
            return ret
        except Exception as e:
            logger.debug(f"[keyreply] 调用 {action} 失败: {e}")
    return None


def _norm_group_id(value) -> str:
    """
    从配置 / 指令里的一项里提取群号。
    WebUI 下拉里显示的是「123456 - 群名」这类带说明的字符串，
    判定生效范围时只取开头的数字群号，两种写法都能正确识别。
    """
    s = str(value or "").strip()
    m = re.search(r"\d{5,}", s)  # QQ 群号至少 5 位，避免误吃群名里的数字
    return m.group(0) if m else s


def _parse_tail_arg(raw: str, prefixes: tuple[str, ...]) -> str:
    """取指令名之后的参数（prefixes 里同时传 - 与 _ 两种写法即可）。"""
    body = (raw or "").strip()
    for prefix in prefixes:
        if body.startswith(prefix):
            body = body[len(prefix):]
            break
    else:
        # 兜底：没有匹配到前缀时，去掉第一段（即命令词本身）
        seg = body.split(None, 1)
        body = seg[1] if len(seg) > 1 else ""
    return body.strip()


# ============================================================
# 插件主类
# ============================================================

class KeyReplyPlugin(Star):
    def __init__(self, context: Context, config: "AstrBotConfig"):
        super().__init__(context)
        self.config = config
        # 读取配置（带默认值，保证未配置时也不崩）
        self.cooldown = int(config.get("cooldown", 0))  # 同一关键词冷却秒数（防刷）

    async def initialize(self):
        """插件加载后调用，用于初始化。"""
        # 初始化本地存储路径（放在数据目录下，跨重启不丢失）
        data_dir = _get_data_dir()
        self._local = _LocalStore(os.path.join(data_dir, LOCAL_RULES_FILE))
        self._groups_local = _LocalStore(os.path.join(data_dir, LOCAL_GROUPS_FILE))
        # bot 所在群聊缓存（群号 + 群名），用于 WebUI 下拉与 /reply-groups
        self._group_cache: list[dict] = self._groups_local.load() or []
        self._group_sync_lock = asyncio.Lock()
        # 探测当前环境 KV 存储是否可用（框架是否注入了 put_kv_data）
        self._kv_available = await self._probe_kv()
        # 生效群聊名单的 KV 备份（面板配置为空时的兜底）
        if self._kv_available:
            try:
                backup = await self.get_kv_data(KV_KEY_GROUPS, [])
                if isinstance(backup, list):
                    self._enabled_cache = backup
            except Exception:
                pass
        rules = await self._load_rules()
        logger.info(f"[keyreply] 关键词回复插件已加载，当前规则数: {len(rules)}")
        logger.info(f"[keyreply] 匹配模式: {self.config.get('match_mode', 'contains')}")
        logger.info(
            f"[keyreply] 规则存储: {'KV + ' if self._kv_available else ''}本地文件={self._local.path}"
        )
        logger.info(
            f"[keyreply] 生效群聊: 模式={self._group_mode()}，已选 {len(self._enabled_group_ids())} 个群"
        )
        # 稍等协议端连上，再自动拉一次群列表写进 WebUI 面板下拉（失败不影响使用）
        try:
            asyncio.create_task(self._delayed_sync_groups())
        except Exception as e:  # pragma: no cover
            logger.warning(f"[keyreply] 群列表自动同步任务启动失败: {e}")

    async def _probe_kv(self) -> bool:
        """探测 put_kv_data / get_kv_data 是否可用。仅探测，不产生告警噪音。"""
        try:
            if not hasattr(self, "put_kv_data") or not hasattr(self, "get_kv_data"):
                return False
            # 用一条临时键做读写往返，确认真能存进去
            probe_key = "__keyreply_probe__"
            await self.put_kv_data(probe_key, True)
            ok = await self.get_kv_data(probe_key, False) is True
            await self.put_kv_data(probe_key, None)
            return ok
        except Exception:
            return False

    # ---------- 生效群聊 ----------
    #
    # 目标：让用户能在 WebUI 面板里「勾选」哪些群启用关键词自动回复。
    # 实现分三步：
    #   1) 通过平台实例拿到 OneBot 客户端，调 get_group_list 读取 bot 所在全部群；
    #   2) 把「群号 - 群名」动态写进 _conf_schema.json 的 enabled_groups.items.options，
    #      面板上即变成可选下拉（拉不到群时保持手填输入框，功能不失效）；
    #   3) 收到消息时按 group_mode（all / whitelist / blacklist）判断当前群是否放行。
    # 无法自动拉群（qq_official 等平台或协议端未连上）时，仍可用 /reply-group-add 手填群号。

    def _group_mode(self) -> str:
        """当前生效模式，非法值一律回退 all（避免配置写错导致全部群失效）。"""
        mode = str(self.config.get("group_mode", "all") or "all").strip().lower()
        return mode if mode in GROUP_MODES else "all"

    def _enabled_group_raw(self) -> list:
        """面板里配置的生效/排除群列表（WebUI 配置优先，其次 KV 备份）。"""
        raw = self.config.get("enabled_groups", []) or []
        if isinstance(raw, dict):
            raw = list(raw.values())
        if isinstance(raw, str):
            raw = [p for p in re.split(r"[,\s;]+", raw) if p]
        if not isinstance(raw, list):
            raw = []
        raw = [str(x).strip() for x in raw if str(x or "").strip()]
        if raw:
            return raw
        return list(getattr(self, "_enabled_cache", []) or [])

    def _enabled_group_ids(self) -> set[str]:
        """归一化后的群号集合（兼容「123456 - 群名」这类带说明的写法）。"""
        return {_norm_group_id(x) for x in self._enabled_group_raw()}

    def _group_allowed(self, group_id) -> bool:
        """判断某个群是否放行（私聊不走这里，由 private_enabled 决定）。"""
        mode = self._group_mode()
        if mode == "all":
            return True
        gid = _norm_group_id(group_id)
        hit = gid in self._enabled_group_ids()
        return hit if mode == "whitelist" else not hit

    def _private_allowed(self) -> bool:
        """私聊是否参与自动回复（群范围限制不影响私聊）。"""
        return bool(self.config.get("private_enabled", True))

    # ---------- 读取 bot 所在群列表 ----------

    def _clients_from_platforms(self) -> list:
        """从平台适配器里拿到所有可用的 OneBot 客户端（aiocqhttp）。"""
        clients: list = []
        try:
            platforms = self.context.platform_manager.get_insts()
        except Exception as e:
            logger.debug(f"[keyreply] 获取平台实例失败: {e}")
            platforms = []
        for platform in platforms or []:
            try:
                getter = getattr(platform, "get_client", None)
                if not callable(getter):
                    continue
                client = getter()
                if client is not None:
                    clients.append(client)
            except Exception as e:
                logger.debug(f"[keyreply] 获取平台客户端失败: {e}")
        return clients

    async def _fetch_group_list(self, event=None) -> list[dict]:
        """调用协议端 API 读取 bot 所在全部群聊，返回 [{group_id, group_name}]。"""
        clients: list = []
        # 优先用当前消息自带的客户端（最贴近发指令的那个 bot 账号）
        if event is not None:
            client = getattr(event, "bot", None)
            if client is not None:
                clients.append(client)
        clients.extend(self._clients_from_platforms())

        groups: list[dict] = []
        seen: set[str] = set()
        for client in clients:
            ret = await _onebot_call(client, "get_group_list")
            if not isinstance(ret, list):
                continue
            for g in ret:
                if not isinstance(g, dict):
                    continue
                gid = str(g.get("group_id") or g.get("group_code") or "").strip()
                if not gid or gid in seen:
                    continue
                seen.add(gid)
                groups.append({"group_id": gid, "group_name": str(g.get("group_name") or "")})
        return groups

    async def _refresh_group_cache(self, event=None) -> list[dict]:
        """刷新群列表缓存 → 存本地 → 同步进 WebUI 下拉。返回最新群列表。"""
        async with self._group_sync_lock:
            groups = await self._fetch_group_list(event)
            if groups:
                self._group_cache = groups
                self._groups_local.save(groups)
                if self._sync_schema_options(groups):
                    logger.info(
                        f"[keyreply] 已同步 {len(groups)} 个群到面板下拉（重载插件后可见）"
                    )
                else:
                    logger.info(f"[keyreply] 已读取 {len(groups)} 个群")
            else:
                logger.info(
                    "[keyreply] 暂未获取到群列表（协议端未连接或非 aiocqhttp 平台），"
                    "可在 QQ 里发送 /reply-groups 重试，或用手填群号"
                )
            return groups

    async def _delayed_sync_groups(self):
        """插件启动后等协议端连上再拉一次群列表。"""
        try:
            await asyncio.sleep(GROUP_SYNC_DELAY)
            if not self._group_cache:
                await self._refresh_group_cache()
            else:
                # 已有缓存也要刷新一次（期间可能加群/退群），并同步到面板
                await self._refresh_group_cache()
        except Exception as e:
            logger.debug(f"[keyreply] 群列表自动同步跳过: {e}")

    def _sync_schema_options(self, groups: list[dict]) -> bool:
        """
        把群列表写进 _conf_schema.json 的 enabled_groups.items.options，
        让面板出现「群号 - 群名」下拉可选项；同时把对照表写进 hint 方便手填。
        只在内容真的变化时才写文件，避免无谓磁盘写入。
        """
        if not self.config.get("auto_sync_group_list", True):
            return False
        try:
            with open(SCHEMA_FILE, encoding="utf-8") as f:
                schema = json.load(f)
        except Exception as e:
            logger.warning(f"[keyreply] 读取 _conf_schema.json 失败，跳过群下拉同步: {e}")
            return False

        node = schema.get("enabled_groups")
        if not isinstance(node, dict):
            return False
        items = node.get("items")
        if not isinstance(items, dict):
            items = {"type": "string", "default": ""}
            node["items"] = items

        if groups:
            options = [
                f"{g['group_id']} - {g['group_name']}" if g.get("group_name") else g["group_id"]
                for g in groups
            ]
            shown = groups[:40]
            hint = [
                "下拉里可直接选群，也可以手填群号（群里发 /sid 可查群号，"
                "/reply-groups 可列出全部群）。当前 bot 所在群："
            ]
            hint += [f"{g['group_id']} = {g['group_name'] or '(未命名)'}" for g in shown]
            if len(groups) > len(shown):
                hint.append(f"...共 {len(groups)} 个群，完整列表见 QQ 里的 /reply-groups")
        else:
            options = None
            hint = [
                "未获取到群列表（协议端未连接？可在 QQ 里发 /reply-groups 重试），"
                "此处可手填群号，群里发 /sid 可查看群号。"
            ]

        if (items.get("options") or None) == (options or None):
            return False
        if options is None:
            items.pop("options", None)
        else:
            items["options"] = options
        node["hint"] = "\n".join(hint)

        try:
            tmp = SCHEMA_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(schema, f, ensure_ascii=False, indent=2)
            os.replace(tmp, SCHEMA_FILE)
            return True
        except Exception as e:
            logger.warning(f"[keyreply] 写回 _conf_schema.json 失败: {e}")
            return False

    # ---------- 生效群聊的保存 ----------

    async def _save_enabled_groups(self, items: list) -> bool:
        """保存生效群列表：写 WebUI 配置（面板可见）+ KV 备份（面板不可用时兜底）。"""
        ok = False
        try:
            self.config["enabled_groups"] = items
            save = getattr(self.config, "save_config", None)
            if callable(save):
                save()
            ok = True
        except Exception as e:
            logger.warning(f"[keyreply] 写回面板配置失败（将只用 KV 备份）: {e}")
        try:
            if getattr(self, "_kv_available", False):
                await self.put_kv_data(KV_KEY_GROUPS, items)
                self._enabled_cache = items
                ok = True
        except Exception as e:
            logger.warning(f"[keyreply] 生效群聊 KV 备份失败: {e}")
        if not ok:
            logger.warning("[keyreply] 生效群聊保存失败：面板配置与 KV 均不可用")
        return ok

    def _group_name(self, gid: str) -> str:
        """按群号取群名（取不到就返回空串）。"""
        for g in getattr(self, "_group_cache", []) or []:
            if str(g.get("group_id")) == str(gid):
                return str(g.get("group_name") or "")
        return ""

    # ---------- 规则持久化 ----------
    #
    # 持久化策略（双保险）：
    #   1) 优先从「插件 KV 存储」读写（框架标准方式，需 AstrBot ≥4.9.2 且环境正常）；
    #   2) 同时 / 降级 读写「本地 JSON 文件」，保证即使 KV 不可用，添加的规则也不丢。
    #   加载时：KV 有数据用 KV，否则用本地文件，再没有才读 WebUI 配置。
    #   保存时：两者都写，任一成功即算成功。

    async def _load_rules(self) -> list[dict]:
        """
        加载规则列表，合并两个来源：
          A. 运行时动态规则：KV 存储 / 本地文件（QQ 指令 add/del 写入的）
          B. WebUI 配置：config["rules"]（在管理面板里添加/编辑的）

        合并策略：以 A 为主（它是最新的真源），再**追加** B 中 A 里没有的关键词规则，
        保证在 WebUI 面板添加的规则也能生效，且两者不互相覆盖。
        """
        # --- 来源 A：运行时动态规则（KV / 本地文件）---
        dynamic: list[dict] = []
        if getattr(self, "_kv_available", False):
            try:
                stored = await self.get_kv_data(KV_KEY_RULES, None)
                if isinstance(stored, list) and stored:
                    dynamic = stored
            except Exception as e:
                logger.warning(f"[keyreply] 读取规则 KV 失败（将改用本地文件）: {e}")
                self._kv_available = False
        if not dynamic:
            local = getattr(self, "_local", None)
            if local is not None:
                data = local.load()
                if isinstance(data, list):
                    dynamic = data

        # 归一化 + 去空
        dynamic = [r for r in dynamic if isinstance(r, dict) and r.get("keyword", "").strip()]

        # --- 来源 B：WebUI 配置 ---
        # template_list 保存后的结构为 [{"__template_key": "basic", "keyword":.., "reply":.., "mode":..}, ...]
        # 这里把 __template_key 等内部字段剥离，只保留业务字段（keyword/reply/mode/remark）
        webui = self.config.get("rules", []) or []
        if isinstance(webui, dict):
            webui = list(webui.values())
        _INTERNAL = {"__template_key", "__template_name"}
        cleaned = []
        for r in webui:
            if not isinstance(r, dict):
                continue
            item = {k: v for k, v in r.items() if k not in _INTERNAL}
            if item.get("keyword", "").strip():
                cleaned.append(item)
        webui = cleaned

        if not dynamic:
            return webui

        # 用关键词集合去重：WebUI 里与动态规则同关键词的会被动态版覆盖
        dyn_keys = {r["keyword"].strip() for r in dynamic}
        merged = list(dynamic)
        for r in webui:
            if r.get("keyword", "").strip() not in dyn_keys:
                merged.append(r)
        return merged

    async def _save_rules(self, rules: list[dict]) -> bool:
        """把规则列表持久化。KV 与本地文件双写，任一成功即返回 True。"""
        ok_kv = await self._save_to_kv(rules)
        ok_local = self._save_to_local(rules)
        if not (ok_kv or ok_local):
            logger.warning("[keyreply] 规则保存失败：KV 与本地文件均不可用")
        return ok_kv or ok_local

    async def _save_to_kv(self, rules: list[dict]) -> bool:
        if not getattr(self, "_kv_available", False):
            return False  # 已探测不可用，静默降级到本地文件
        try:
            await self.put_kv_data(KV_KEY_RULES, rules)
            return True
        except Exception as e:
            logger.warning(f"[keyreply] KV 保存失败（将使用本地备份）: {e}")
            self._kv_available = False
            return False

    def _save_to_local(self, rules: list[dict]) -> bool:
        local = getattr(self, "_local", None)
        if local is None:
            return False
        return local.save(rules)

    # ---------- 权限辅助 ----------

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """判断是否有权执行管理指令。ADMIN_IDS 为空则不限制。"""
        if not ADMIN_IDS:
            return True
        sid = str(event.get_sender_id() or "")
        return sid in [str(x) for x in ADMIN_IDS]

    # ---------- 消息监听 ----------

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """监听所有消息（群 + 私聊），命中关键词即回复。"""
        text = (event.message_str or "").strip()
        if not text:
            return

        # 忽略以命令前缀开头的消息，避免和指令系统冲突
        if text.startswith("/"):
            return

        # ★生效范围过滤：群聊按 group_mode 判断，私聊单独看 private_enabled
        group_id = event.get_group_id()
        if group_id:
            if not self._group_allowed(group_id):
                return
        elif not self._private_allowed():
            return

        rules = await self._load_rules()
        if not rules:
            return

        global_mode = self.config.get("match_mode", "contains")

        for rule in rules:
            keyword = rule.get("keyword", "").strip()
            reply = rule.get("reply", "").strip()
            if not keyword or not reply:
                continue

            mode = rule.get("mode", global_mode) or global_mode
            if not _match(keyword, text, mode):
                continue

            # 命中：记录持久化（计数 + 最近命中时间）
            await self._record_hit(keyword, event)

            # 是否 @ 发送者（群聊时生效）
            at_prefix = ""
            if self.config.get("at_sender", True) and event.get_group_id():
                sender = event.get_sender_name() or event.get_sender_id()
                at_prefix = f"@{sender} "

            yield event.plain_result(f"{at_prefix}{reply}".strip())

            # 一条消息只触发第一条命中的规则（避免一嗓子触发一堆回复）
            return

    # ---------- 持久化（命中统计） ----------

    async def _record_hit(self, keyword: str, event: AstrMessageEvent):
        """把命中记录写入插件 KV 存储。"""
        try:
            import time
            key = f"hit:{keyword}"
            record = await self.get_kv_data(key, {"count": 0, "last": 0})
            if not isinstance(record, dict):
                record = {"count": 0, "last": 0}
            record["count"] = int(record.get("count", 0)) + 1
            record["last"] = int(time.time())
            await self.put_kv_data(key, record)
        except Exception as e:
            # 持久化失败不影响主流程回复
            logger.warning(f"[keyreply] 命中记录写入失败: {e}")

    # ============================================================
    # 管理指令（★新增：运行时动态增删规则）
    # ============================================================

    @filter.command("reply-add")
    @filter.command("reply_add")
    async def reply_add(self, event: AstrMessageEvent):
        """
        添加一条关键词规则。用法（关键词/回复中含空格时用 | 分隔）：
          /reply-add 你好 | 你好呀~ | contains
          /reply-add 谢谢 | 不客气！
        仅管理员可用（ADMIN_IDS 留空则不限制）。
        """
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 你没有权限管理关键词规则。")
            return

        # 兼容命令后带空格或不带空格
        raw = event.message_str or ""
        parsed = _parse_add_args(raw)
        if parsed is None:
            yield event.plain_result(
                "⚠️ 参数格式错误。正确用法：\n"
                "/reply-add <关键词> | <回复内容> [| 模式]\n"
                "模式可选：contains / exact / regex"
            )
            return

        keyword, reply, mode = parsed
        if mode not in VALID_MODES:
            yield event.plain_result(
                f"⚠️ 不支持的模式「{mode}」，可选：{', '.join(VALID_MODES)}"
            )
            return

        rules = await self._load_rules()

        # 去重：已存在相同关键词则覆盖其回复（视为更新）
        updated = False
        for rule in rules:
            if rule.get("keyword", "").strip() == keyword:
                rule["reply"] = reply
                rule["mode"] = mode
                updated = True
                break
        if not updated:
            rules.append({"keyword": keyword, "reply": reply, "mode": mode})

        if await self._save_rules(rules):
            tag = "更新" if updated else "添加"
            yield event.plain_result(
                f"✅ 已{tag}规则：[{mode}] {keyword} => {reply}"
            )
        else:
            yield event.plain_result("❌ 规则保存失败，请查看后端日志。")

    @filter.command("reply-del")
    @filter.command("reply_del")
    async def reply_del(self, event: AstrMessageEvent):
        """
        删除一条关键词规则。用法：
          /reply-del 你好
        """
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 你没有权限管理关键词规则。")
            return

        raw = (event.message_str or "").strip()
        keyword = raw.split(None, 1)[1].strip() if " " in raw else ""
        if not keyword:
            yield event.plain_result("⚠️ 用法：/reply-del <关键词>")
            return

        rules = await self._load_rules()
        new_rules = [r for r in rules if r.get("keyword", "").strip() != keyword]
        if len(new_rules) == len(rules):
            yield event.plain_result(f"⚠️ 未找到关键词「{keyword}」，删除失败。")
            return

        if await self._save_rules(new_rules):
            yield event.plain_result(f"✅ 已删除关键词「{keyword}」的规则。")
        else:
            yield event.plain_result("❌ 规则保存失败，请查看后端日志。")

    @filter.command("reply-list")
    @filter.command("reply_list")
    async def reply_list(self, event: AstrMessageEvent):
        """列出当前所有关键词规则。用法：/reply-list"""
        rules = await self._load_rules()
        if not rules:
            yield event.plain_result("当前未配置任何关键词规则。")
            return

        global_mode = self.config.get("match_mode", "contains")
        # ★采用多行布局：每条规则占三行（序号/模式 + 关键词 + 回复），
        #   关键词或回复内容再长也会在自己那一行内自动换行，不会被挤到屏幕外。
        lines = [f"📋 当前关键词规则（共 {len(rules)} 条）："]
        for i, rule in enumerate(rules, 1):
            kw = rule.get("keyword", "")
            rp = rule.get("reply", "")
            mode = rule.get("mode", global_mode)
            lines.append(f"  {i}. [{mode}]")
            lines.append(f"       关键词: {kw}")
            lines.append(f"       回复: {rp}")
        yield event.plain_result("\n".join(lines))

    @filter.command("reply-stats")
    @filter.command("reply_stats")
    async def reply_stats(self, event: AstrMessageEvent):
        """查看各关键词的命中统计。用法：/reply-stats"""
        rules = await self._load_rules()
        if not rules:
            yield event.plain_result("当前未配置任何关键词规则。")
            return

        lines = ["📊 关键词命中统计："]
        for rule in rules:
            keyword = rule.get("keyword", "")
            record = await self.get_kv_data(f"hit:{keyword}", {"count": 0, "last": 0})
            count = record.get("count", 0) if isinstance(record, dict) else 0
            lines.append(f"  · {keyword} → 命中 {count} 次")
        yield event.plain_result("\n".join(lines))

    # ============================================================================
    # 生效群聊指令（★v1.4 新增）
    # ============================================================================

    def _save_config_value(self, key: str, value) -> bool:
        """把单个配置项写回面板（用于切换生效模式等）。"""
        try:
            self.config[key] = value
            save = getattr(self.config, "save_config", None)
            if callable(save):
                save()
            return True
        except Exception as e:
            logger.warning(f"[keyreply] 保存配置项 {key} 失败: {e}")
            return False

    def _mode_desc(self, mode: str) -> str:
        return {
            "all": "全部群聊生效（名单不生效）",
            "whitelist": "仅名单内群聊生效",
            "blacklist": "名单内群聊不生效，其余都生效",
        }.get(mode, mode)

    @filter.command("reply-groups")
    @filter.command("reply_groups")
    async def reply_groups(self, event: AstrMessageEvent):
        """列出 bot 所在全部群聊并标注是否生效。用法：/reply-groups"""
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 你没有权限管理生效群聊。")
            return

        groups = await self._refresh_group_cache(event)
        if not groups:
            yield event.plain_result(
                "⚠️ 没能读取到群列表。\n"
                "常见原因：当前不是 aiocqhttp（OneBot）平台，或协议端还没连上。\n"
                "你可以直接手填群号：/reply-group-add 123456\n"
                "（在目标群里发 /sid 就能看到群号）"
            )
            return

        mode = self._group_mode()
        ids = self._enabled_group_ids()
        lines = [
            f"📋 bot 所在群聊（共 {len(groups)} 个）",
            f"当前模式：{mode} —— {self._mode_desc(mode)}",
            "",
        ]
        for i, g in enumerate(groups[:GROUP_LIST_MAX_SHOWN], 1):
            gid, name = g["group_id"], (g.get("group_name") or "(未命名)")
            if mode == "all":
                mark = "✅"
            elif mode == "whitelist":
                mark = "✅" if gid in ids else "➖"
            else:
                mark = "🚫" if gid in ids else "✅"
            lines.append(f"{i}. {mark} {gid}  {name}")
        if len(groups) > GROUP_LIST_MAX_SHOWN:
            lines.append(f"...还有 {len(groups) - GROUP_LIST_MAX_SHOWN} 个群未显示")
        lines.append("")
        lines.append(
            "指令：/reply-group-add <群号>（群里直接发则加当前群）\n"
            "/reply-group-del <群号>\n"
            "/reply-scope whitelist|blacklist|all"
        )
        yield event.plain_result("\n".join(lines))

    @filter.command("reply-group-add")
    @filter.command("reply_group_add")
    async def reply_group_add(self, event: AstrMessageEvent):
        """把群加入生效/排除名单。用法：/reply-group-add [群号]"""
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 你没有权限管理生效群聊。")
            return

        arg = _parse_tail_arg(event.message_str or "", ("/reply-group-add", "/reply_group_add"))
        gid = _norm_group_id(arg) if arg else ""
        if not gid:  # 没给群号就用当前群（私聊里则报错）
            gid = _norm_group_id(event.get_group_id() or "")
        if not gid:
            yield event.plain_result(
                "⚠️ 用法：/reply-group-add <群号>\n（在目标群里直接发这条指令则默认加当前群）"
            )
            return

        items = list(self._enabled_group_raw())
        if gid in {_norm_group_id(x) for x in items}:
            yield event.plain_result(f"ℹ️ 群 {gid} 已经在名单里了。")
        else:
            name = self._group_name(gid)
            items.append(f"{gid} - {name}" if name else gid)
            if not await self._save_enabled_groups(items):
                yield event.plain_result("❌ 保存失败，请查看后端日志。")
                return
            yield event.plain_result(f"✅ 已把群 {gid}{f'（{name}）' if name else ''} 加入名单。")

        if self._group_mode() == "all":
            yield event.plain_result(
                "⚠️ 当前模式仍是 all（全部群生效），名单暂不起作用。\n"
                "只想让名单内的群生效请发：/reply-scope whitelist\n"
                "想屏蔽名单内的群请发：/reply-scope blacklist"
            )

    @filter.command("reply-group-del")
    @filter.command("reply_group_del")
    async def reply_group_del(self, event: AstrMessageEvent):
        """把群移出生效/排除名单。用法：/reply-group-del <群号>"""
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 你没有权限管理生效群聊。")
            return

        arg = _parse_tail_arg(event.message_str or "", ("/reply-group-del", "/reply_group_del"))
        gid = _norm_group_id(arg) if arg else ""
        if not gid:
            gid = _norm_group_id(event.get_group_id() or "")
        if not gid:
            yield event.plain_result("⚠️ 用法：/reply-group-del <群号>")
            return

        items = list(self._enabled_group_raw())
        new_items = [x for x in items if _norm_group_id(x) != gid]
        if len(new_items) == len(items):
            yield event.plain_result(f"⚠️ 名单里没有群 {gid}，删除失败。")
            return

        if await self._save_enabled_groups(new_items):
            yield event.plain_result(f"✅ 已把群 {gid} 移出名单。")
        else:
            yield event.plain_result("❌ 保存失败，请查看后端日志。")

    @filter.command("reply-scope")
    @filter.command("reply_scope")
    async def reply_scope(self, event: AstrMessageEvent):
        """查看/切换生效范围模式。用法：/reply-scope [all|whitelist|blacklist]"""
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 你没有权限管理生效群聊。")
            return

        arg = _parse_tail_arg(
            event.message_str or "", ("/reply-scope", "/reply_scope")
        ).strip().lower()

        if not arg:
            mode = self._group_mode()
            ids = self._enabled_group_ids()
            lines = [
                f"📍 当前生效模式：{mode}",
                f"   {self._mode_desc(mode)}",
                f"名单内共 {len(ids)} 个群：{', '.join(sorted(ids)) if ids else '（空）'}",
                f"私聊自动回复：{'开' if self._private_allowed() else '关'}",
                "",
                "切换：/reply-scope whitelist（仅名单内） / blacklist（排除名单内） / all（全部群）",
            ]
            yield event.plain_result("\n".join(lines))
            return

        if arg not in GROUP_MODES:
            yield event.plain_result(
                f"⚠️ 不支持的模式「{arg}」，可选：{', '.join(GROUP_MODES)}"
            )
            return

        if self._save_config_value("group_mode", arg):
            yield event.plain_result(f"✅ 生效模式已切换为 {arg} —— {self._mode_desc(arg)}")
        else:
            yield event.plain_result("❌ 保存失败，请查看后端日志。")

    async def terminate(self):
        """插件卸载/停用时调用。"""
        logger.info("[keyreply] 关键词回复插件已卸载。")
