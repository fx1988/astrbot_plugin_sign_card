from __future__ import annotations

import asyncio
import base64
import html
import json
import random
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools


PLUGIN_NAME = "astrbot_plugin_sign_card"
DEFAULT_BACKGROUND_DIR = "/AstrBot/data/plugin_data/astrbot_plugin_sign_card/backgrounds"
QUOTE_POOL = [
    "哪有顷刻之间的心灰意冷，有的，只是日积月累的看透罢了。",
    "今天也要把普通的日子过得闪闪发光。",
    "慢一点没关系，重要的是还在向前走。",
    "愿你遇到的所有温柔，都刚刚好。",
    "生活的答案藏在每一次认真签到里。",
]


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _escape(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _data_uri(content: bytes, content_type: str = "image/jpeg") -> str:
    return f"data:{content_type};base64,{base64.b64encode(content).decode('ascii')}"


def _placeholder_uri(label: str, color: str = "#92a6d8") -> str:
    safe_label = html.escape(label[:2], quote=True)
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' width='160' height='160' "
        f"viewBox='0 0 160 160'><rect width='160' height='160' rx='80' fill='{color}'/>"
        f"<text x='80' y='96' text-anchor='middle' font-size='56' fill='white'>{safe_label}</text></svg>"
    )
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode("ascii")


class SignCardPlugin(Star):
    """Independent glassmorphism sign-in card plugin."""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.data_file = self.data_dir / "sign_card_data.json"
        self.cache_dir = self.data_dir / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.background_dir = Path(
            str(self.config.get("background_dir") or DEFAULT_BACKGROUND_DIR)
        )
        self.sign_keyword = str(self.config.get("sign_keyword") or "签到").strip().lstrip("/") or "签到"
        self.auto_install_browser = bool(self.config.get("auto_install_browser", True))
        try:
            configured_opacity = float(self.config.get("panel_opacity", 0.76))
        except (TypeError, ValueError):
            configured_opacity = 0.76
        self.panel_opacity = max(0.0, min(1.0, configured_opacity))
        self._data_lock = asyncio.Lock()
        self._render_lock = asyncio.Lock()
        self._data = self._migrate_data(self._load_data())
        self._save_data()
        self._playwright = None
        self._browser = None
        logger.info(f"{PLUGIN_NAME}: sign keyword is /{self.sign_keyword}")

    def _load_data(self) -> dict[str, Any]:
        if not self.data_file.exists():
            return {"schema_version": 3, "users": {}, "groups": {}}
        try:
            value = json.loads(self.data_file.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception as exc:
            logger.warning(f"{PLUGIN_NAME}: failed to load data: {exc}")
            return {}

    @staticmethod
    def _migrate_data(value: dict[str, Any]) -> dict[str, Any]:
        if (
            value.get("schema_version") in (2, 3)
            and isinstance(value.get("users"), dict)
            and isinstance(value.get("groups"), dict)
        ):
            value["schema_version"] = 3
            for user in value["users"].values():
                if isinstance(user, dict):
                    user.setdefault("first_sign_count", 0)
            for group in value["groups"].values():
                if not isinstance(group, dict):
                    continue
                members = group.get("members", {})
                if not isinstance(members, dict):
                    continue
                for member in members.values():
                    if isinstance(member, dict):
                        member.pop("first_sign_count", None)
                        member.pop("last_seen_date", None)
            return value

        migrated: dict[str, Any] = {"schema_version": 3, "users": {}, "groups": {}}
        old_groups = value.get("groups", {}) if isinstance(value, dict) else {}
        if not isinstance(old_groups, dict):
            return migrated

        for group_id, old_group in old_groups.items():
            if not isinstance(old_group, dict):
                continue
            new_group = {
                "name": str(old_group.get("name") or ""),
                "members": {},
            }
            old_users = old_group.get("users", {})
            if not isinstance(old_users, dict):
                old_users = {}
            for user_id, old_user in old_users.items():
                if not isinstance(old_user, dict):
                    continue
                uid = str(user_id)
                global_user = migrated["users"].setdefault(
                    uid,
                    {
                        "user_id": uid,
                        "name": str(old_user.get("name") or uid),
                        "total_days": 0,
                        "streak": 0,
                        "score": 0,
                        "exp": 0,
                        "last_date": "",
                        "history": {},
                        "first_sign_count": 0,
                    },
                )
                global_user["name"] = str(old_user.get("name") or global_user["name"])
                for key in ("total_days", "streak", "score", "exp"):
                    global_user[key] = max(
                        _safe_int(global_user.get(key)), _safe_int(old_user.get(key))
                    )
                old_last_date = str(old_user.get("last_date") or "")
                if old_last_date > str(global_user.get("last_date") or ""):
                    global_user["last_date"] = old_last_date
                old_history = old_user.get("history", {})
                if isinstance(old_history, dict):
                    global_user.setdefault("history", {}).update(old_history)
                new_group["members"][uid] = {
                    "name": str(old_user.get("name") or uid),
                    "join_time": _safe_int(old_user.get("join_time")),
                }
            migrated["groups"][str(group_id)] = new_group
        return migrated

    def _save_data(self) -> None:
        temp_file = self.data_file.with_suffix(".tmp")
        temp_file.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temp_file.replace(self.data_file)

    @staticmethod
    def _group_key(event: AstrMessageEvent) -> str:
        group_id = str(event.get_group_id() or "").strip()
        return group_id if group_id else f"private:{event.get_sender_id()}"

    @staticmethod
    def _today() -> str:
        return date.today().isoformat()

    @staticmethod
    def _greeting() -> str:
        hour = datetime.now().hour
        if hour < 6:
            return "凌晨好"
        if hour < 11:
            return "早上好"
        if hour < 14:
            return "中午好"
        if hour < 19:
            return "下午好"
        return "晚上好"

    def _ensure_global_user(
        self,
        users: dict[str, Any],
        user_id: str,
        user_name: str,
    ) -> dict[str, Any]:
        user = users.setdefault(
            user_id,
            {
                "user_id": user_id,
                "name": user_name,
                "total_days": 0,
                "streak": 0,
                "score": 0,
                "exp": 0,
                "last_date": "",
                "history": {},
                "first_sign_count": 0,
            },
        )
        user["name"] = user_name or user.get("name") or user_id
        user.setdefault("history", {})
        user.setdefault("first_sign_count", 0)
        return user

    @staticmethod
    def _ensure_group_member(
        group: dict[str, Any], user_id: str, user_name: str
    ) -> dict[str, Any]:
        members = group.setdefault("members", {})
        member = members.setdefault(
            user_id,
            {
                "name": user_name,
                "join_time": 0,
            },
        )
        member["name"] = user_name or member.get("name") or user_id
        return member

    async def _bot_call(self, event: AstrMessageEvent, method: str, **kwargs):
        try:
            func = getattr(event.bot, method, None)
            if callable(func):
                result = await func(**kwargs)
            else:
                result = await event.bot.api.call_action(method, **kwargs)
            if isinstance(result, dict) and isinstance(result.get("data"), (dict, list)):
                return result["data"]
            return result
        except Exception as exc:
            logger.debug(f"{PLUGIN_NAME}: OneBot {method} failed: {exc}")
            return None

    async def _get_member_info(self, event: AstrMessageEvent, group_id: str, user_id: str) -> dict[str, Any]:
        if not group_id.isdigit() or not user_id.isdigit():
            return {}
        result = await self._bot_call(
            event,
            "get_group_member_info",
            group_id=int(group_id),
            user_id=int(user_id),
            no_cache=False,
        )
        return result if isinstance(result, dict) else {}

    async def _get_group_info(self, event: AstrMessageEvent, group_id: str) -> dict[str, Any]:
        if not group_id.isdigit():
            return {}
        result = await self._bot_call(
            event, "get_group_info", group_id=int(group_id), no_cache=False
        )
        return result if isinstance(result, dict) else {}

    async def _get_group_members(self, event: AstrMessageEvent, group_id: str) -> list[dict[str, Any]]:
        if not group_id.isdigit():
            return []
        result = await self._bot_call(
            event, "get_group_member_list", group_id=int(group_id)
        )
        return result if isinstance(result, list) else []

    @staticmethod
    async def _fetch_data_uri(url: str, fallback_label: str) -> str:
        def fetch() -> bytes:
            request = Request(url, headers={"User-Agent": "AstrBot-sign-card/1.0"})
            with urlopen(request, timeout=5) as response:
                return response.read()

        try:
            content = await asyncio.to_thread(fetch)
            if content:
                return _data_uri(content)
        except Exception:
            pass
        return _placeholder_uri(fallback_label)

    async def _background_uri(self) -> str:
        candidates = (
            [
                path
                for path in self.background_dir.iterdir()
                if path.is_file()
                and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
            ]
            if self.background_dir.exists()
            else []
        )
        if not candidates:
            return _placeholder_uri("签", "#596a9c")
        path = random.choice(candidates)
        try:
            content = await asyncio.to_thread(path.read_bytes)
            content_type = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
            return _data_uri(content, content_type)
        except Exception:
            return _placeholder_uri("签", "#596a9c")

    @staticmethod
    def _format_join_time(timestamp: Any) -> str:
        value = _safe_int(timestamp)
        if value <= 0:
            return "--/--/--"
        try:
            return datetime.fromtimestamp(value).strftime("%y/%m/%d")
        except (OverflowError, OSError, ValueError):
            return "--/--/--"

    @staticmethod
    def _level(exp: int) -> tuple[int, int, int]:
        level = max(1, exp // 100 + 1)
        progress = exp % 100
        return level, progress, 100

    @staticmethod
    def _rank(
        members: dict[str, Any],
        users: dict[str, Any],
        user_id: str,
        key: str,
    ) -> int:
        if key == "score":
            def score_metric(user: dict[str, Any]) -> tuple[int, int, int]:
                return (
                    _safe_int(user.get("score")),
                    -_safe_int(user.get("first_sign_count")),
                    _safe_int(user.get("streak")),
                )

            target = score_metric(users.get(user_id, {}))
            return 1 + sum(
                score_metric(users.get(uid, {})) > target for uid in members
            )

        ordered = sorted(
            ((uid, users.get(uid, {})) for uid in members),
            key=lambda item: (
                _safe_int(item[1].get(key)),
                _safe_int(item[1].get("total_days")),
                item[0],
            ),
            reverse=True,
        )
        for index, (uid, _) in enumerate(ordered, start=1):
            if uid == user_id:
                return index
        return len(ordered) or 1

    async def _build_view(self, event: AstrMessageEvent) -> dict[str, Any]:
        group_id = self._group_key(event)
        user_id = str(event.get_sender_id() or "unknown")
        raw_name = str(event.get_sender_name() or user_id).strip()
        member_info, group_info, members = await asyncio.gather(
            self._get_member_info(event, group_id, user_id),
            self._get_group_info(event, group_id),
            self._get_group_members(event, group_id),
        )
        display_name = str(
            member_info.get("card") or member_info.get("nickname") or raw_name or user_id
        ).strip()
        if display_name:
            raw_name = display_name

        async with self._data_lock:
            users = self._data.setdefault("users", {})
            groups = self._data.setdefault("groups", {})
            group = groups.setdefault(group_id, {"members": {}, "name": ""})
            user = self._ensure_global_user(users, user_id, raw_name)
            member = self._ensure_group_member(group, user_id, raw_name)
            join_time = _safe_int(member_info.get("join_time"))
            if join_time:
                member["join_time"] = join_time
            if group_info.get("group_name"):
                group["name"] = str(group_info["group_name"])
            today = self._today()
            already_signed = user.get("last_date") == today
            if not already_signed:
                yesterday = (date.today() - timedelta(days=1)).isoformat()
                previous_date = str(user.get("last_date") or "")
                if previous_date == yesterday:
                    user["streak"] = _safe_int(user.get("streak")) + 1
                else:
                    user["streak"] = 1
                    if previous_date:
                        user["first_sign_count"] = (
                            _safe_int(user.get("first_sign_count")) + 1
                        )
                user["total_days"] = _safe_int(user.get("total_days")) + 1
                user["score"] = _safe_int(user.get("score")) + 5
                user["exp"] = _safe_int(user.get("exp")) + 1
                user["last_date"] = today
                history = user.setdefault("history", {})
                history[today] = True
                cutoff = (date.today() - timedelta(days=120)).isoformat()
                user["history"] = {
                    key: value for key, value in history.items() if key >= cutoff
                }
            self._save_data()

            level, progress, level_target = self._level(_safe_int(user.get("exp")))
            group_members = group.get("members", {})
            score_rank = self._rank(group_members, users, user_id, "score")
            level_rank = self._rank(group_members, users, user_id, "exp")
            signed_days = dict(user.get("history", {}))
            user_snapshot = dict(user)
            member_snapshot = dict(member)
            group_name_from_data = str(group.get("name") or "")

        group_name = str(group_info.get("group_name") or group_name_from_data or "本群")
        member_count = (
            _safe_int(group_info.get("member_count"))
            or len(members)
            or len(group_members)
        )
        bg_uri, avatar_uri, group_avatar_uri = await asyncio.gather(
            self._background_uri(),
            self._fetch_data_uri(
                f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640", raw_name
            ),
            self._fetch_data_uri(
                f"https://p.qlogo.cn/gh/{group_id}/{group_id}/640/", group_name
            ),
        )

        calendar = []
        for offset in range(6, -1, -1):
            day = date.today() - timedelta(days=offset)
            day_key = day.isoformat()
            calendar.append(
                {
                    "label": day.strftime("%m/%d"),
                    "signed": bool(signed_days.get(day_key)),
                    "today": offset == 0,
                }
            )
        now = datetime.now()
        return {
            "background": bg_uri,
            "avatar": avatar_uri,
            "group_avatar": group_avatar_uri,
            "user_name": raw_name,
            "user_id": user_id,
            "group_id": group_id,
            "group_name": group_name,
            "member_count": member_count,
            "greeting": self._greeting(),
            "date": now.strftime("%m/%d"),
            "quote": random.choice(QUOTE_POOL),
            "streak": _safe_int(user_snapshot.get("streak")),
            "total_days": _safe_int(user_snapshot.get("total_days")),
            "join_time": self._format_join_time(member_snapshot.get("join_time")),
            "first_sign_count": _safe_int(user_snapshot.get("first_sign_count")),
            "score": _safe_int(user_snapshot.get("score")),
            "level": level,
            "progress": progress,
            "level_target": level_target,
            "score_rank": score_rank,
            "level_rank": level_rank,
            "calendar": calendar,
            "signed_today": already_signed,
            "reward_score": 0 if already_signed else 5,
            "reward_exp": 0 if already_signed else 1,
            "panel_opacity": self.panel_opacity,
        }

    def _build_html(self, view: dict[str, Any]) -> str:
        panel_opacity = max(0.0, min(1.0, float(view.get("panel_opacity", 0.76))))
        subpanel_opacity = min(1.0, panel_opacity * 0.75)
        calendar_opacity = min(1.0, panel_opacity * 0.82)
        group_opacity = min(1.0, panel_opacity * 0.66)
        calendar_html = "".join(
            f"<div class='day {'today' if cell['today'] else ''} {'done' if cell['signed'] else ''}'>"
            f"<span>{_escape(cell['label'])}</span><b>{'✓' if cell['signed'] else '×'}</b></div>"
            for cell in view["calendar"]
        )
        status = "今天已经签到过了哦" if view["signed_today"] else "今日签到成功"
        reward_title = (
            "今日已签到 · 本次不重复奖励"
            if view["signed_today"]
            else f"今日积分 +{view['reward_score']} · 经验 +{view['reward_exp']}"
        )
        return f"""<!doctype html>
<html lang='zh-CN'><head><meta charset='utf-8'><style>
*{{box-sizing:border-box}}html,body{{margin:0;width:900px;height:1125px;overflow:hidden}}
body{{font-family:'Noto Sans SC','Microsoft YaHei','WenQuanYi Zen Hei',Arial,sans-serif;color:#633a2d}}
.canvas{{position:relative;width:900px;height:1125px;padding-top:280px;background:#67728f url('{view['background']}') center/cover no-repeat}}
.canvas:before{{content:'';position:absolute;inset:0;background:rgba(17,25,52,.28)}}
.card{{position:relative;margin:0 40px;width:820px;padding:28px 34px 26px;border:1px solid rgba(255,255,255,.72);border-radius:28px;background:rgba(255,255,255,{panel_opacity:.3f});box-shadow:0 18px 40px rgba(20,24,47,.22);backdrop-filter:blur(14px)}}
.quote{{min-height:42px;padding:6px 8px 18px;font-size:22px;font-weight:700;line-height:1.45;letter-spacing:.3px}}
.identity{{display:flex;align-items:center;gap:22px;padding:0 8px 20px}}
.avatar{{width:92px;height:92px;border:5px solid rgba(255,255,255,.9);border-radius:50%;object-fit:cover;box-shadow:0 4px 12px rgba(47,39,44,.15)}}
.identity-main{{flex:1;min-width:0}}.greeting{{font-size:42px;font-weight:800;line-height:1.1;letter-spacing:1px}}.name{{margin-top:10px;font-size:21px;font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.star{{display:inline-flex;width:46px;height:46px;margin-left:8px;align-items:center;justify-content:center;border-radius:50%;color:#668aff;background:rgba(211,222,255,.8);font-size:37px;vertical-align:middle}}
.streak-box{{width:150px;height:108px;padding:16px 18px;border-radius:20px;background:rgba(255,255,255,{subpanel_opacity:.3f});font-size:18px;font-weight:700}}.streak-box b{{display:inline-block;margin-top:3px;color:#ff5a76;font-size:40px;line-height:1}}.streak-box span{{margin-left:5px;font-size:20px;color:#633a2d}}.cal-icon{{position:relative;display:inline-block;width:31px;height:28px;margin-left:8px;border:2px solid #ff8090;border-radius:5px;vertical-align:2px}}.cal-icon:before{{content:'';position:absolute;left:-2px;right:-2px;top:6px;border-top:2px solid #ff8090}}.cal-icon:after{{content:'{datetime.now().day}';position:absolute;left:0;right:0;top:7px;text-align:center;color:#ff6d82;font-size:12px;line-height:18px;font-style:normal}}
.stats,.rankings{{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;padding:17px 16px;border-radius:20px;background:rgba(255,255,255,{subpanel_opacity:.3f})}}
.stat,.rank{{display:flex;align-items:center;gap:12px;min-width:0}}.round{{display:flex;flex:0 0 54px;width:54px;height:54px;align-items:center;justify-content:center;border-radius:50%;background:rgba(255,204,126,.28);color:#e9a044;font-size:28px;font-weight:800}}.stat-label,.rank-label{{font-size:17px;font-weight:700;white-space:nowrap}}.stat-value,.rank-value{{margin-top:2px;font-size:27px;font-weight:800;white-space:nowrap}}.rank-value{{font-size:25px}}
.section-title{{display:flex;justify-content:space-between;align-items:baseline;margin:24px 6px 12px;font-size:22px;font-weight:800}}.section-title small{{font-size:16px;color:#8e6a5d}}
.calendar{{display:grid;grid-template-columns:repeat(7,1fr);gap:12px}}.day{{height:88px;padding:14px 7px;text-align:center;border:1px solid rgba(255,255,255,.85);border-radius:14px;background:rgba(255,255,255,{calendar_opacity:.3f});color:#a27867}}.day span{{display:block;font-size:16px;font-weight:700}}.day b{{display:block;margin-top:6px;font-size:35px;line-height:1;color:#c7a391}}.day.done b{{color:#eba343}}.day.today{{background:#ff5875;color:white;border-color:#ff5875;box-shadow:0 5px 12px rgba(255,88,117,.22)}}.day.today b{{color:white}}
.footer{{display:grid;grid-template-columns:1.6fr 1fr;gap:14px;margin-top:22px;padding:16px;border-radius:19px;background:rgba(255,255,255,{subpanel_opacity:.3f})}}.reward-title{{font-size:20px;font-weight:800}}.reward-sub{{margin-top:5px;font-size:15px;font-weight:700}}.bar{{height:11px;margin-top:12px;border-radius:8px;background:#d8d1d0;overflow:hidden}}.bar i{{display:block;width:{view['progress']}%;height:100%;border-radius:8px;background:#ff9677}}.reward-foot{{margin-top:6px;font-size:14px;color:#8d6c61}}.group{{display:flex;align-items:center;gap:12px;padding:10px 12px;border:1px solid rgba(255,255,255,.7);border-radius:16px;background:rgba(255,255,255,{group_opacity:.3f})}}.group img{{width:56px;height:56px;border-radius:50%;object-fit:cover}}.group strong{{display:block;font-size:17px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}.group span{{display:block;margin-top:5px;font-size:13px;line-height:1.4;color:#896658}}
</style></head><body><div class='canvas'><main class='card'>
<div class='quote'>{_escape(view['quote'])}</div>
<section class='identity'><img class='avatar' src='{view['avatar']}'><div class='identity-main'><div class='greeting'>{_escape(view['greeting'])}<span class='star'>☆</span></div><div class='name'>『 {_escape(view['user_name'])} 』({_escape(view['user_id'])})</div></div><div class='streak-box'>连续签到<br><b>{view['streak']}</b><span>天</span><i class='cal-icon'></i></div></section>
<section class='stats'><div class='stat'><i class='round'>✓</i><div><div class='stat-label'>累计签到</div><div class='stat-value'>{view['total_days']}<small> 天</small></div></div></div><div class='stat'><i class='round'>♧</i><div><div class='stat-label'>入群时间</div><div class='stat-value'>{_escape(view['join_time'])}</div></div></div><div class='stat'><i class='round'>◷</i><div><div class='stat-label'>连续签到</div><div class='stat-value'>{view['streak']}<small> 天</small></div></div></div></section>
<div class='section-title'><span>签到日历</span><small>本月已签到 {view['total_days']} 天</small></div><section class='calendar'>{calendar_html}</section>
<div class='section-title'><span>群内排名</span><small>{_escape(status)}</small></div><section class='rankings'><div class='rank'><i class='round'>¥</i><div><div class='rank-label'>积分排名</div><div class='rank-value'>第 {view['score_rank']} 名</div></div></div><div class='rank'><i class='round' style='color:#ff7893;background:rgba(255,160,187,.3)'>Lv</i><div><div class='rank-label'>等级排名</div><div class='rank-value'>第 {view['level_rank']} 名</div></div></div><div class='rank'><i class='round' style='color:#ff7893;background:rgba(255,160,187,.3)'>1st</i><div><div class='rank-label'>首签次数</div><div class='rank-value'>{view['first_sign_count']}<small> 次</small></div></div></div></section>
<section class='footer'><div><div class='reward-title'>{_escape(reward_title)}</div><div class='reward-sub'>当前等级 Lv.{view['level']} · 当前积分 {view['score']} · 当前经验 {view['progress']}</div><div class='bar'><i></i></div><div class='reward-foot'>签到时间 {datetime.now().strftime('%Y/%m/%d %H:%M:%S')}</div></div><div class='group'><img src='{view['group_avatar']}'><div><strong>{_escape(view['group_name'])}</strong><span>群号 {_escape(view['group_id'])}<br>群成员 {view['member_count']} 人</span></div></div></section>
</main></div></body></html>"""

    async def _ensure_browser(self):
        if self._browser is not None:
            return self._browser
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        launch_args = ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        executable = str(self.config.get("browser_executable_path") or "").strip()
        launch_kwargs: dict[str, Any] = {"headless": True, "args": launch_args}
        if executable:
            launch_kwargs["executable_path"] = executable
        try:
            self._browser = await self._playwright.chromium.launch(**launch_kwargs)
        except Exception as exc:
            if executable:
                launch_kwargs.pop("executable_path", None)
                try:
                    self._browser = await self._playwright.chromium.launch(**launch_kwargs)
                    return self._browser
                except Exception as fallback_exc:
                    exc = fallback_exc
            if not self.auto_install_browser or not self._browser_is_missing(exc):
                raise exc
            await self._install_browser()
            self._browser = await self._playwright.chromium.launch(**launch_kwargs)
        return self._browser

    @staticmethod
    def _browser_is_missing(exc: Exception) -> bool:
        message = str(exc).lower()
        return "executable doesn't exist" in message or "playwright install" in message

    @staticmethod
    async def _install_browser() -> None:
        logger.warning(
            f"{PLUGIN_NAME}: Chromium is missing; downloading Playwright headless shell"
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "playwright",
            "install",
            "--only-shell",
            "chromium",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=900)
        except TimeoutError as exc:
            process.kill()
            await process.communicate()
            raise RuntimeError("Chromium download timed out after 15 minutes") from exc
        message = output.decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            raise RuntimeError(
                f"Chromium installation failed with code {process.returncode}: {message[-3000:]}"
            )
        logger.info(f"{PLUGIN_NAME}: Playwright headless shell installed successfully")

    async def _render(self, view: dict[str, Any]) -> Path:
        async with self._render_lock:
            browser = await self._ensure_browser()
            filename = self.cache_dir / f"sign_{view['group_id']}_{view['user_id']}_{int(time.time() * 1000)}.png"
            page = await browser.new_page(
                viewport={"width": 900, "height": 1125}, device_scale_factor=1
            )
            try:
                await page.set_content(self._build_html(view), wait_until="load")
                await page.screenshot(path=str(filename), full_page=True, type="png")
            finally:
                await page.close()
            cutoff = time.time() - 3 * 86400
            for old in self.cache_dir.glob("sign_*.png"):
                if old != filename:
                    try:
                        if old.stat().st_mtime < cutoff:
                            old.unlink()
                    except OSError:
                        pass
            return filename

    async def _handle_sign(self, event: AstrMessageEvent):
        try:
            view = await self._build_view(event)
            image_path = await self._render(view)
            yield event.image_result(str(image_path))
        except Exception as exc:
            logger.exception(f"{PLUGIN_NAME}: sign card failed: {exc}")
            yield event.plain_result("签到卡片生成失败，请稍后再试。")

    def _matches_sign_keyword(self, event: AstrMessageEvent) -> bool:
        message = str(event.message_str or "").strip()
        if message.startswith("/"):
            message = message[1:].strip()
        return bool(message) and message == self.sign_keyword

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000)
    async def sign(self, event: AstrMessageEvent):
        if not self._matches_sign_keyword(event):
            return
        event.stop_event()
        async for result in self._handle_sign(event):
            yield result

    async def terminate(self):
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self._browser = None
        self._playwright = None
