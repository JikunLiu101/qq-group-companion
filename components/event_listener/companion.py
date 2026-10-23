from __future__ import annotations

import asyncio
import base64
import dataclasses
import json
import logging
import secrets
import pathlib
import random
import re
import time
import typing

import langbot_plugin.api.entities.builtin.platform.message as platform_message
from langbot_plugin.api.definition.components.common.event_listener import EventListener
from langbot_plugin.api.entities import context, events

from companion.buffer import MessageBuffer, Record
from companion.decisions import log_decision
from companion.faces import face_name
from companion.formatter import addressees, format_chain
from companion.log import apply_level, get_logger, preview
from companion.gate import (
    NOTICE_SENT,
    OVER_USER_LIMIT,
    PASSED,
    STRENGTH,
    STRONG,
    TRIGGER_AT,
    TRIGGER_PRIVATE,
    TRIGGER_REPLY,
    Limits,
    check_limits,
    detect_trigger,
    gate_regexp,
    make_marker,
    notice_already_sent,
    parse_aliases,
    read_marker,
    start_of_today,
    strip_marker,
)
from companion.media import DEFAULT_MAX_BYTES, DEFAULT_MAX_IMAGES, collect_images, purge_inbox, save_images
from companion.output_filter import (
    DEFAULT_MAX_CHARS,
    PASS_TOKEN,
    clean_reply,
    contains_email,
    drop_final_period,
    extract_error,
    extract_say,
)
from companion.prompt import DEFAULT_MAX_MESSAGES, DEFAULT_WINDOW_SECONDS, build_prompt, select_context
from companion.stickers import StickerLibrary, parse_reply
from companion.whitelist import is_allowed, parse_whitelist

logger = get_logger(__name__)

MODE_LISTEN = 'listen'
MODE_MENTION = 'mention'
INBOX_PURGE_INTERVAL = 3600
NICKNAME_LOOKUP_TIMEOUT = 5.0
PRIVATE_PREFIX = 'private-'  # 白名单私聊在缓冲里的"群号"前缀
PRIVATE_CHAT_NAME = '与主人的私聊'
TRIGGER_NAMES = {'at': '@', 'reply': '回复', 'name': '提到名字', 'owner': '提到主人', 'private': '私聊'}
LIMIT_NAMES = {'user-limit': '今天的强点名次数已用完', 'weak-rate': '本群这一分钟的弱点名次数已用完',
               'group-daily-limit': '本群今天的回复已达上限', 'total-daily-limit': '今天的总回复已达上限'}

# 强点名超出每日次数时，插件直接回的话术（不调用 Claude）。{n} 会替换成每日次数。
DEFAULT_NOTICES = (
    '你今天都找了我 {n} 次了，你不累我都累了',
    '今日额度已用光，改日再来 @ 我吧',
    '……今天的份到此为止 明天请早',
    '我的时间很贵的，你今天约满了',
)


# Claude 出错时（包装脚本输出 <qgc-error kind="..."/>），插件按错误类型回的话术
DEFAULT_ERROR_REPLIES = {
    'limit': (
        '下午茶时间到了，有事明天请早',
        '今日营业结束',
        '……累了，今天不接客',
    ),
    'refused': (
        '这个不答，换一个',
        '这种问题我才不接',
    ),
    'other': (
        '……刚走神了，再说一遍',
        '没听清，再说一次',
    ),
}
ERROR_KIND_LABELS = {'limit': '额度用完或被锁定', 'refused': '安全机制拒答', 'other': '其他错误'}
ERROR_TRIGGER_TTL = 600  # 点名记录保留多久（秒），用来在出错时知道是强点名还是弱点名


def _flag(value: typing.Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def _int(config: dict[str, typing.Any], key: str, default: int, minimum: int = 0) -> int:
    try:
        value = int(config.get(key, default))
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def _notices(raw: typing.Any) -> tuple[str, ...]:
    items = [line.strip() for line in re.split(r'[\n|]+', str(raw or '')) if line.strip()]
    return tuple(items) or DEFAULT_NOTICES


def _replies(raw: typing.Any, default: tuple[str, ...]) -> tuple[str, ...]:
    items = [line.strip() for line in re.split(r'[\n|]+', str(raw or '')) if line.strip()]
    return tuple(items) or default


@dataclasses.dataclass(frozen=True)
class Settings:
    whitelist: frozenset[str]
    data_dir: str
    mode: str
    self_qq: str
    persona_name: str
    aliases: list[str]
    limits: Limits
    max_chars: int
    gate_secret: str = ''
    owner_qq: str = ''
    owner_aliases: tuple[str, ...] = ()
    nickname_as_alias: bool = True
    nickname_refresh_seconds: int = 600
    notices: tuple[str, ...] = DEFAULT_NOTICES
    context_max_messages: int = DEFAULT_MAX_MESSAGES
    context_window_seconds: int = DEFAULT_WINDOW_SECONDS
    vision_enabled: bool = False
    inbox_dir: str = ''
    max_images: int = DEFAULT_MAX_IMAGES
    max_image_bytes: int = DEFAULT_MAX_BYTES
    inbox_retention_days: int = 3
    stickers_enabled: bool = False
    stickers_dir: str = ''
    sticker_daily_limit: int = 10
    sticker_repeat_seconds: int = 30 * 60
    qq_faces_enabled: bool = False
    error_replies: dict[str, tuple[str, ...]] = dataclasses.field(default_factory=lambda: dict(DEFAULT_ERROR_REPLIES))
    error_reply_weak: bool = False
    error_cooldown_seconds: int = 10 * 60
    private_whitelist: frozenset[str] = frozenset()
    reply_delay_min: float = 3.0
    reply_delay_max: float = 12.0
    log_level: str = 'INFO'

    @classmethod
    def from_config(cls, config: dict[str, typing.Any]) -> Settings:
        persona_name = str(config.get('persona-name') or '').strip()
        aliases = parse_aliases(config.get('persona-aliases'))
        if len(persona_name) >= 2 and persona_name not in aliases:
            aliases.insert(0, persona_name)
        mode = str(config.get('reply-mode') or MODE_LISTEN).strip()
        return cls(
            whitelist=parse_whitelist(config.get('group-whitelist')),
            data_dir=str(config.get('data-dir') or '').strip(),
            mode=mode if mode in (MODE_LISTEN, MODE_MENTION) else MODE_LISTEN,
            self_qq=str(config.get('self-qq') or '').strip(),
            persona_name=persona_name,
            aliases=aliases,
            limits=Limits.from_config(config),
            max_chars=max(20, _int(config, 'max-reply-chars', DEFAULT_MAX_CHARS, 1)),
            gate_secret=str(config.get('gate-secret') or '').strip(),
            owner_qq=str(config.get('owner-qq') or '').strip(),
            owner_aliases=tuple(parse_aliases(config.get('owner-aliases'))),
            nickname_as_alias=_flag(config.get('nickname-as-alias', True)),
            nickname_refresh_seconds=_int(config, 'nickname-refresh-minutes', 10, 1) * 60,
            notices=_notices(config.get('over-limit-replies')),
            context_max_messages=_int(config, 'context-max-messages', DEFAULT_MAX_MESSAGES, 1),
            context_window_seconds=_int(config, 'context-window-minutes', DEFAULT_WINDOW_SECONDS // 60, 1) * 60,
            vision_enabled=_flag(config.get('vision-enabled', False)),
            inbox_dir=str(config.get('inbox-dir') or '').strip(),
            max_images=_int(config, 'max-images', DEFAULT_MAX_IMAGES, 1),
            max_image_bytes=_int(config, 'max-image-mb', DEFAULT_MAX_BYTES // (1024 * 1024), 1) * 1024 * 1024,
            inbox_retention_days=_int(config, 'inbox-retention-days', 3, 1),
            stickers_enabled=_flag(config.get('stickers-enabled', False)),
            stickers_dir=str(config.get('stickers-dir') or '').strip(),
            sticker_daily_limit=_int(config, 'daily-sticker-limit-per-group', 10),
            sticker_repeat_seconds=_int(config, 'sticker-repeat-minutes', 30) * 60,
            qq_faces_enabled=_flag(config.get('qq-faces-enabled', False)),
            error_replies={
                kind: _replies(config.get(f'error-replies-{kind}'), default)
                for kind, default in DEFAULT_ERROR_REPLIES.items()
            },
            error_reply_weak=_flag(config.get('error-reply-weak', False)),
            error_cooldown_seconds=_int(config, 'error-cooldown-minutes', 10) * 60,
            private_whitelist=parse_whitelist(config.get('private-whitelist')),
            reply_delay_min=float(_int(config, 'reply-delay-min-seconds', 3)),
            reply_delay_max=float(_int(config, 'reply-delay-max-seconds', 12)),
            log_level=str(config.get('log-level') or 'INFO'),
        )

    @property
    def can_reply(self) -> bool:
        # 没有暗号密钥就无法和流水线群规则配合，一律不回复
        return self.mode == MODE_MENTION and bool(self.self_qq) and len(self.gate_secret) >= 16


_reported_config: dict[str, str | None] = {'key': None}  # 插件进程内上次检查过的配置（插件入口和监听器共用）


def load_settings(raw: dict[str, typing.Any]) -> Settings:
    """读配置；第一次读到或配置有变化时，检查并写日志。"""
    settings = Settings.from_config(raw)
    key = json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str)
    if key != _reported_config['key']:
        _reported_config['key'] = key
        CompanionEventListener._report_config(settings)
    return settings


def _message_id(event: typing.Any) -> str:
    source = next((c for c in event.message_chain if getattr(c, 'type', '') == 'Source'), None)
    return str(getattr(source, 'id', None) or f'{event.sender_id}-{getattr(event.message_event, "time", "")}')


def _sender_name(event: typing.Any) -> str:
    sender = getattr(event.message_event, 'sender', None)
    # 群成员用群名片；私聊好友用昵称
    return str(getattr(sender, 'member_name', '') or getattr(sender, 'nickname', '') or '')


def _group_name(event: typing.Any) -> str:
    sender = getattr(event.message_event, 'sender', None)
    return str(getattr(getattr(sender, 'group', None), 'name', '') or '')


def _plain_text(chain: typing.Any) -> str:
    return ''.join(str(getattr(c, 'text', '') or '') for c in chain if getattr(c, 'type', '') == 'Plain')


def _block(ctx: context.EventContext) -> None:
    ctx.prevent_default()
    ctx.prevent_postorder()


class CompanionEventListener(EventListener):
    """挂载点：

    - GroupMessageReceived：白名单 → 转文字（含"对谁说"）→ 写缓冲 → 判断强/弱点名和额度 →
      放行时在消息开头插入暗号；强点名超额时回一句预设话术（每人每天一次）
    - GroupNormalMessageReceived：校验暗号 → 拼上下文（近 2 小时、最多 40 条）→ 交给 Claude
    - NormalMessageResponded：Claude 出错（<qgc-error/>）→ 强点名回预设话术、弱点名不回；
      <pass/> 不发；只发 <say> 里的内容；表情标记换成表情；去链接、去 Markdown、限长；含邮箱不发；记下自己的发言
    - GroupCommandSent：白名单群里的 LangBot 命令一律不执行
    - Person*：私聊白名单（private-whitelist）里的人走同一套处理，每条都算强点名；其他私聊（含微信）原样放过

    reply-mode=listen（默认）时与 P2 完全一样：只记录，一律不放行。
    """

    def __init__(self):
        super().__init__()
        self._buffer: MessageBuffer | None = None
        self._buffer_dir: str | None = None
        self._stickers: StickerLibrary | None = None
        self._last_inbox_purge = 0.0
        self._nicknames: dict[str, tuple[str, float]] = {}  # 群号 -> (机器人在该群的群昵称, 查询时间)
        self._pending: dict[tuple[str, str], tuple[str, float]] = {}  # (群号, 发言人) -> (点名类型, 交给 Claude 的时间)
        self._last_error_reply: dict[str, float] = {}  # 群号 -> 上次回错误话术的时间

    # ---------- 资源 ----------

    def _get_buffer(self, data_dir: str) -> MessageBuffer | None:
        if not data_dir:
            return None
        if self._buffer is None or self._buffer_dir != data_dir:
            if self._buffer is not None:
                self._buffer.close()
            self._buffer = MessageBuffer(pathlib.Path(data_dir))
            self._buffer_dir = data_dir
        return self._buffer

    def _settings(self) -> Settings:
        return load_settings(self.get_plugin_config() or {})

    @staticmethod
    def _report_config(settings: Settings) -> None:
        """配置（第一次读到或有变化时）：设日志级别，汇报当前设置，检查需要人处理的问题。"""
        level = apply_level(settings.log_level)
        logger.info(
            '配置已加载：模式=%s，白名单群 %d 个，私聊白名单 %d 人，看图=%s，表情包=%s，QQ 表情=%s，日志级别=%s',
            '点名回复' if settings.mode == MODE_MENTION else '只旁听', len(settings.whitelist),
            len(settings.private_whitelist), settings.vision_enabled, settings.stickers_enabled,
            settings.qq_faces_enabled, level,
        )
        if not settings.data_dir:
            logger.error('没有设置数据目录（data-dir）：白名单群的消息不会被记录，也不会回复。请在插件配置里填一个本机绝对路径。')
        if not settings.whitelist and not settings.private_whitelist:
            logger.warning('群白名单和私聊白名单都是空的：插件不会处理任何消息。')
        if settings.mode != MODE_MENTION:
            return
        if not settings.self_qq:
            logger.error('点名回复模式需要填写机器人 QQ 号（self-qq），否则不会回复。')
        if len(settings.gate_secret) < 16:
            suggestion = secrets.token_urlsafe(24)
            logger.error(
                '点名回复模式需要至少 16 位的放行暗号密钥（gate-secret），现在不会回复。可以用这个随机密钥：%s ；'
                '对应的流水线群响应规则正则（原样粘贴）：%s',
                suggestion, json.dumps(gate_regexp(suggestion)),
            )
        else:
            logger.info('流水线「触发 → 群响应规则 → 正则」应为（原样粘贴，含一个看不见的字符）：%s',
                        json.dumps(gate_regexp(settings.gate_secret)))
        if settings.vision_enabled and not settings.inbox_dir:
            logger.error('开启了看图，但没有设置图片 inbox 目录（inbox-dir）：不会保存图片。')
        if settings.stickers_enabled:
            if not settings.stickers_dir or not pathlib.Path(settings.stickers_dir).is_dir():
                logger.error('开启了表情包，但表情库目录（stickers-dir）不存在：%s', settings.stickers_dir or '（未填写）')
            elif not StickerLibrary(settings.stickers_dir).tags():
                logger.warning('表情库目录里没有可用的图片：%s', settings.stickers_dir)
        if settings.reply_delay_min > settings.reply_delay_max:
            logger.warning('回复前随机等待的最短时间（%s 秒）大于最长时间（%s 秒），将按最短时间等待。',
                           int(settings.reply_delay_min), int(settings.reply_delay_max))

    def _self_nickname(self, group_id: str) -> str:
        cached = self._nicknames.get(group_id)
        return cached[0] if cached else ''

    async def _refresh_self_nickname(self, ctx: context.EventContext, settings: Settings, group_id: str) -> None:
        """查一下机器人在这个群里现在的群昵称（群名片优先），按群缓存；失败时沿用旧值。"""
        cached = self._nicknames.get(group_id)
        if cached and time.time() - cached[1] < settings.nickname_refresh_seconds:
            return
        name = cached[0] if cached else ''
        try:
            bot_uuid = await asyncio.wait_for(ctx.get_bot_uuid(), NICKNAME_LOOKUP_TIMEOUT)
            result = await asyncio.wait_for(
                self.plugin.call_platform_api(
                    bot_uuid, 'get_group_member_info', {'group_id': group_id, 'user_id': settings.self_qq}
                ),
                NICKNAME_LOOKUP_TIMEOUT,
            )
            if isinstance(result, dict):
                user = result.get('user') or {}
                name = str(result.get('display_name') or user.get('remark') or user.get('nickname') or name).strip()
        except Exception:
            logger.warning('查询群 %s 里机器人的群昵称失败，沿用旧值「%s」', group_id, name, exc_info=True)
        if name != (cached[0] if cached else ''):
            logger.info('机器人在群 %s 的群昵称：%s', group_id, name or '（空）')
        self._nicknames[group_id] = (name, time.time())

    def _aliases_for(self, settings: Settings, group_id: str) -> list[str]:
        aliases = list(settings.aliases)
        nickname = self._self_nickname(group_id)
        if settings.nickname_as_alias and len(nickname) >= 2 and nickname not in aliases:
            aliases.append(nickname)
        return aliases

    def _name_of(self, settings: Settings, buffer: MessageBuffer, group_id: str):
        def name_of(qq: str) -> str | None:
            if settings.self_qq and qq == settings.self_qq:
                return self._self_nickname(group_id) or settings.persona_name or '你'
            return buffer.name_of(group_id, qq)

        return name_of

    def _sticker_library(self, settings: Settings) -> StickerLibrary | None:
        if not (settings.stickers_enabled and settings.stickers_dir):
            return None
        if self._stickers is None or self._stickers.directory != pathlib.Path(settings.stickers_dir):
            self._stickers = StickerLibrary(settings.stickers_dir)
        return self._stickers

    def _pick_sticker(
        self, settings: Settings, buffer: MessageBuffer, group_id: str, tags: list[str]
    ) -> tuple[str, bytes] | None:
        """只看第一个表情标签：存在、未超每日上限、近期没发过，才返回 (标签, 图片内容)。"""
        library = self._sticker_library(settings)
        if library is None or not tags:
            return None
        tag = tags[0]
        path = library.path_of(tag)
        if path is None:
            logger.info('模型想发表情包「%s」，但表情库里没有，已忽略', tag)
            return None
        now = time.time()
        if buffer.count_media_since(group_id, 'sticker', start_of_today(now)) >= settings.sticker_daily_limit:
            logger.info('%s 今天的表情包已达上限（%d），不发「%s」', group_id, settings.sticker_daily_limit, tag)
            return None
        if buffer.media_sent_since(group_id, 'sticker', tag, now - settings.sticker_repeat_seconds):
            logger.debug('表情包「%s」最近发过，这次不发', tag)
            return None
        try:
            return tag, path.read_bytes()
        except OSError:
            logger.warning('读取表情包文件失败：%s', path, exc_info=True)
            return None

    async def _save_images(self, settings: Settings, chain: typing.Any) -> list[str]:
        if not (settings.vision_enabled and settings.inbox_dir):
            return []
        now = time.time()
        if now - self._last_inbox_purge > INBOX_PURGE_INTERVAL:
            self._last_inbox_purge = now
            try:
                purge_inbox(settings.inbox_dir, settings.inbox_retention_days, now)
            except OSError:
                logger.warning('清理 inbox 失败', exc_info=True)
        images = collect_images(chain, settings.max_images)
        saved = await save_images(images, settings.inbox_dir, max_bytes=settings.max_image_bytes)
        if len(saved) < len(images):
            logger.warning('消息里有 %d 张图，只保存了 %d 张（来源不在腾讯图片域名、下载失败、太大或不是图片）',
                           len(images), len(saved))
        elif saved:
            logger.debug('已保存 %d 张图片到 inbox', len(saved))
        return [str(path) for path in saved]

    def _record_self(self, settings: Settings, buffer: MessageBuffer, group_id: str, text: str, reply_to: str) -> None:
        buffer.add(
            group_id=group_id,
            message_id=f'self-{time.time_ns()}',
            sender_id=settings.self_qq,
            sender_name=settings.persona_name,
            text=text,
            is_self=True,
            reply_to=reply_to,
        )

    async def _send_over_limit_notice(
        self, ctx: context.EventContext, settings: Settings, buffer: MessageBuffer, group_id: str, user_id: str
    ) -> None:
        """强点名超额：每人每天只提醒一次，之后当天再叫就不理。"""
        if notice_already_sent(buffer, group_id, user_id):
            return
        notice = drop_final_period(random.choice(settings.notices).replace('{n}', str(settings.limits.per_user_strong_daily)))
        try:
            await ctx.reply(platform_message.MessageChain([platform_message.Plain(text=notice)]), quote_origin=True)
        except Exception:
            logger.warning('发送超额提醒失败', exc_info=True)
            return
        buffer.record_trigger(group_id, user_id, 'strong', NOTICE_SENT)
        self._record_self(settings, buffer, group_id, notice, buffer.name_of(group_id, user_id) or user_id)
        logger.info('%s 用户 %s 今天的强点名次数已用完，已回超额话术：%s', group_id, user_id, notice)

    async def _deliver(
        self, ctx: context.EventContext, settings: Settings, chain: platform_message.MessageChain, *, quote: bool
    ) -> bool:
        """插件自己发回复：只有群里的强点名才引用原消息；随机等几秒再发。

        LangBot 的引用开关是整条流水线统一的，没法按消息区分，所以由插件发、再拦掉默认发送。
        发送失败时退回给 LangBot 按流水线配置发送。返回是否由插件发出。
        """
        delay = random.uniform(settings.reply_delay_min, max(settings.reply_delay_min, settings.reply_delay_max))
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            await ctx.reply(chain, quote_origin=quote)
        except Exception:
            logger.warning('插件发送回复失败，改由 LangBot 按流水线配置发送', exc_info=True)
            ctx.event.reply_message_chain = chain
            return False
        logger.debug('已发送（等待 %.1f 秒，%s引用原消息）', delay, '' if quote else '不')
        _block(ctx)
        return True

    async def _reply_error(
        self,
        ctx: context.EventContext,
        settings: Settings,
        buffer: MessageBuffer | None,
        group_id: str,
        user_id: str,
        trigger: str | None,
        kind: str,
    ) -> None:
        """Claude 出错：强点名回一句预设话术（同一群冷却期内只回一次），弱点名默认不回。"""
        strong = trigger is not None and STRENGTH.get(trigger) == STRONG
        now = time.time()
        if not (settings.can_reply and buffer is not None):
            reason = 'cannot-reply'
        elif not (strong or settings.error_reply_weak):
            reason = 'weak-silent'
        elif now - self._last_error_reply.get(group_id, 0.0) < settings.error_cooldown_seconds:
            reason = 'cooldown'
        else:
            reason = ''
        if reason:
            logger.warning('%s 模型出错（%s），按规则不回话术（%s）', group_id, ERROR_KIND_LABELS.get(kind, kind),
                           {'cannot-reply': '当前不能回复', 'weak-silent': '弱点名', 'cooldown': '冷却中'}[reason])
            log_decision(settings.data_dir, 'responded', group=group_id, outcome='error-silent',
                         error=kind, trigger=trigger, reason=reason)
            return _block(ctx)

        text = drop_final_period(random.choice(settings.error_replies.get(kind) or settings.error_replies['other']))
        self._last_error_reply[group_id] = now
        await self._deliver(ctx, settings, platform_message.MessageChain([platform_message.Plain(text=text)]),
                            quote=self._should_quote(group_id, trigger))
        self._record_self(settings, buffer, group_id, text, buffer.name_of(group_id, user_id) or user_id)
        logger.warning('%s 模型出错（%s），已回出错话术：%s', group_id, ERROR_KIND_LABELS.get(kind, kind), text)
        log_decision(settings.data_dir, 'responded', group=group_id, outcome='error-reply',
                     error=kind, trigger=trigger, text=text)

    # ---------- 挂载点 ----------

    async def initialize(self):
        await super().initialize()

        @self.handler(events.GroupMessageReceived)
        async def on_group_message(ctx: context.EventContext):
            settings = self._settings()
            group_id = str(ctx.event.launcher_id)
            if not is_allowed(group_id, settings.whitelist):
                logger.debug('群 %s 不在白名单，忽略', group_id)
                return _block(ctx)
            await self._guard(ctx, '收到群消息', self._on_received(ctx, settings, group_id, private=False))

        @self.handler(events.PersonMessageReceived)
        async def on_person_message(ctx: context.EventContext):
            # 微信私聊也会触发这个事件：不在私聊白名单里就原样放过，什么都不做
            settings = self._settings()
            key = self._private_key(settings, ctx.event)
            if key is None:
                return
            await self._guard(ctx, '收到私聊', self._on_received(ctx, settings, key, private=True))

        @self.handler(events.GroupNormalMessageReceived)
        async def on_group_normal_message(ctx: context.EventContext):
            settings = self._settings()
            group_id = str(ctx.event.launcher_id)
            await self._guard(ctx, '准备交给模型', self._on_normal(
                ctx, settings, group_id, allowed=is_allowed(group_id, settings.whitelist), private=False))

        @self.handler(events.PersonNormalMessageReceived)
        async def on_person_normal_message(ctx: context.EventContext):
            settings = self._settings()
            key = self._private_key(settings, ctx.event)
            if key is None:
                return
            await self._guard(ctx, '准备交给模型', self._on_normal(ctx, settings, key, allowed=True, private=True))

        @self.handler(events.GroupCommandSent)
        async def on_group_command(ctx: context.EventContext):
            # 群友发 "@机器人 !xxx" 会被 LangBot 当成命令：在白名单群里一律不执行
            if is_allowed(str(ctx.event.launcher_id), self._settings().whitelist):
                logger.info('群 %s 有人发了 LangBot 命令，已拦截（白名单群里不执行命令）', ctx.event.launcher_id)
                _block(ctx)

        @self.handler(events.PersonCommandSent)
        async def on_person_command(ctx: context.EventContext):
            if self._private_key(self._settings(), ctx.event) is not None:
                logger.info('私聊 %s 发了 LangBot 命令，已拦截', ctx.event.launcher_id)
                _block(ctx)

        @self.handler(events.NormalMessageResponded)
        async def on_responded(ctx: context.EventContext):
            event = ctx.event
            settings = self._settings()
            if event.launcher_type == 'group':
                key = str(event.launcher_id)
                if not is_allowed(key, settings.whitelist):
                    return
            else:
                key = self._private_key(settings, event)
                if key is None:
                    return  # 其他私聊（例如微信）不归本插件管
            await self._guard(ctx, '处理模型回复', self._on_responded(ctx, settings, key))

    async def _guard(self, ctx: context.EventContext, stage: str, work: typing.Awaitable[None]) -> None:
        """意外异常：记 ERROR 并拦下这条消息（宁可不回，也不能把没过滤的内容发出去）。"""
        try:
            await work
        except Exception:
            logger.exception('%s时出错，这条消息已拦下（%s %s）。这是插件的问题，请把这段日志反馈给开发者。',
                             stage, getattr(ctx.event, 'launcher_type', ''), getattr(ctx.event, 'launcher_id', ''))
            _block(ctx)

    # ---------- 处理逻辑（群聊和白名单私聊共用） ----------

    @staticmethod
    def _should_quote(group_id: str, trigger: str | None) -> bool:
        """群里被 @ 或被回复（强点名）才引用原消息；弱点名和私聊像普通聊天一样直接发。"""
        return not group_id.startswith(PRIVATE_PREFIX) and trigger in (TRIGGER_AT, TRIGGER_REPLY)

    def _private_key(self, settings: Settings, event: typing.Any) -> str | None:
        """白名单私聊在缓冲里用 private-<QQ号> 作为"群号"；不在白名单返回 None。"""
        user = str(getattr(event, 'launcher_id', '') or '')
        if user and user in settings.private_whitelist:
            return f'{PRIVATE_PREFIX}{user}'
        return None

    async def _on_received(self, ctx: context.EventContext, settings: Settings, group_id: str, *, private: bool) -> None:
        """第一个挂载点：记录 → 判断点名 → 额度 → 放行时插入暗号。group_id 对私聊是 private-<QQ号>。"""
        event = ctx.event
        buffer = self._get_buffer(settings.data_dir)
        if buffer is None:
            logger.debug('没有设置数据目录，%s 的消息未记录', group_id)
            return _block(ctx)

        if not private and settings.can_reply and settings.nickname_as_alias:
            await self._refresh_self_nickname(ctx, settings, group_id)
        name_of = self._name_of(settings, buffer, group_id)
        message_id = _message_id(event)
        ts = getattr(event.message_event, 'time', None)
        buffer.add(
            group_id=group_id,
            message_id=message_id,
            sender_id=event.sender_id,
            sender_name=_sender_name(event),
            text=format_chain(event.message_chain, name_of=name_of),
            ts=float(ts) if ts else None,
            reply_to='、'.join(addressees(event.message_chain, name_of=name_of)),
        )

        logger.debug('记录 %s 的消息 %s：%s', group_id, message_id, preview(_plain_text(event.message_chain)))
        if not settings.can_reply:
            logger.debug('当前不回复（模式=%s，机器人 QQ 号%s，暗号密钥%s），只记录', settings.mode,
                         '已填' if settings.self_qq else '未填', '已设' if len(settings.gate_secret) >= 16 else '未设或太短')
            return _block(ctx)
        if private:
            trigger = TRIGGER_PRIVATE  # 白名单私聊：每条都算在叫她
        else:
            trigger = detect_trigger(
                event.message_chain,
                settings.self_qq,
                self._aliases_for(settings, group_id),
                owner_aliases=settings.owner_aliases,
                sender_id=event.sender_id,
                owner_qq=settings.owner_qq,
            )
        if trigger is None:
            logger.debug('%s 消息 %s 没有点名，不回复', group_id, message_id)
            return _block(ctx)
        text_preview = _plain_text(event.message_chain)

        user_id = str(event.sender_id)
        strength = STRENGTH[trigger]
        # 私聊不占每人每天的强点名次数，但仍受每日总量兜底
        limits = dataclasses.replace(settings.limits, per_user_strong_daily=10**9) if private else settings.limits
        limited = check_limits(buffer, group_id, user_id, trigger, limits)
        if limited is not None:
            buffer.record_trigger(group_id, user_id, strength, limited)
            logger.info('%s 用户 %s 点名（%s）未放行：%s', group_id, user_id, TRIGGER_NAMES.get(trigger, trigger),
                        LIMIT_NAMES.get(limited, limited))
            if limited == OVER_USER_LIMIT:
                await self._send_over_limit_notice(ctx, settings, buffer, group_id, user_id)
            log_decision(settings.data_dir, 'received', group=group_id, message_id=message_id, user=user_id,
                         trigger=trigger, outcome=limited, text=text_preview)
            return _block(ctx)

        buffer.record_trigger(group_id, user_id, strength, PASSED)
        # 放行：在 Source 之后插入暗号。群聊靠流水线群规则只认它；私聊没有群规则，由第二个挂载点校验
        marker = platform_message.Plain(text=make_marker(settings.gate_secret, trigger, message_id))
        components = list(event.message_chain)
        index = 1 if components and getattr(components[0], 'type', '') == 'Source' else 0
        components.insert(index, marker)
        ctx.event.message_chain = platform_message.MessageChain(components)
        logger.info('放行：%s 用户 %s（%s）：%s', group_id, user_id, TRIGGER_NAMES.get(trigger, trigger), preview(text_preview))
        log_decision(settings.data_dir, 'received', group=group_id, message_id=message_id, user=user_id,
                     trigger=trigger, outcome='passed', nickname=self._self_nickname(group_id), text=text_preview)

    async def _on_normal(
        self, ctx: context.EventContext, settings: Settings, group_id: str, *, allowed: bool, private: bool
    ) -> None:
        """第二个挂载点：校验暗号 → 拼上下文 → 交给 Claude。"""
        event = ctx.event
        buffer = self._get_buffer(settings.data_dir)
        plain = _plain_text(event.message_chain)
        # 消息 ID 从暗号里取：到这一步 LangBot 已经丢掉了 Source
        marker = read_marker(plain, settings.gate_secret)
        trigger = marker.trigger if marker else None
        message_id = marker.message_id if marker else _message_id(event)
        # 只认插件自己放行的消息（暗号逐条校验）；其他任何情况都不交给 Claude
        if not (settings.can_reply and allowed and buffer and trigger):
            if not allowed:
                logger.warning('%s 不在白名单，却进了这条流水线，已拦截。请检查 LangBot 机器人的事件路由。', group_id)
            elif settings.can_reply and buffer and not trigger:
                logger.warning('%s 收到一条没有有效放行暗号的消息，已拦截。如果经常出现，请检查流水线群响应规则：'
                               '应该只填插件给出的正则，并关闭 @、前缀和随机。', group_id)
            else:
                logger.debug('%s 当前不回复，拦截交给模型', group_id)
            log_decision(
                settings.data_dir, 'normal', group=group_id, message_id=message_id,
                outcome='blocked', can_reply=settings.can_reply, mode=settings.mode,
                allowed=allowed, has_buffer=buffer is not None,
                trigger=trigger, has_marker='QGC:' in plain, has_source=any(
                    getattr(c, 'type', '') == 'Source' for c in event.message_chain),
                chain=[getattr(c, 'type', '') for c in event.message_chain], text=strip_marker(plain),
            )
            return _block(ctx)

        name_of = self._name_of(settings, buffer, group_id)
        clean_chain = [
            c if getattr(c, 'type', '') != 'Plain' else platform_message.Plain(text=strip_marker(c.text))
            for c in event.message_chain
        ]
        current = buffer.get(group_id, message_id)
        if current is None:
            ts = getattr(event.message_event, 'time', None)
            current = Record(
                group_id=group_id,
                message_id=message_id,
                sender_id=str(event.sender_id),
                sender_name=_sender_name(event),
                ts=float(ts) if ts else time.time(),
                text=format_chain(clean_chain, name_of=name_of),
                is_self=False,
            )
        quote = next((c for c in clean_chain if getattr(c, 'type', '') == 'Quote'), None)
        quoted = ''
        if quote is not None:
            who = name_of(str(getattr(quote, 'sender_id', '') or '')) or str(getattr(quote, 'sender_id', '') or '某人')
            quoted = f'{who}：{format_chain(getattr(quote, "origin", None) or [], name_of=name_of)}'

        library = self._sticker_library(settings)
        ctx.event.user_message_alter = build_prompt(
            group_name=PRIVATE_CHAT_NAME if private else _group_name(event),
            trigger=trigger,
            context=select_context(
                buffer,
                group_id,
                max_messages=settings.context_max_messages,
                window_seconds=settings.context_window_seconds,
            ),
            current=current,
            quoted=quoted,
            image_paths=await self._save_images(settings, clean_chain),
            sticker_tags=library.tags() if library else [],
            owner_qq=settings.owner_qq,
            self_nickname=self._self_nickname(group_id),
        )
        now = time.time()
        self._pending = {k: v for k, v in self._pending.items() if now - v[1] < ERROR_TRIGGER_TTL}
        self._pending[(group_id, str(event.sender_id))] = (trigger, now)
        logger.info('交给模型：%s（%s，输入 %d 字）', group_id, TRIGGER_NAMES.get(trigger, trigger),
                    len(ctx.event.user_message_alter))
        logger.debug('给模型的输入：\n%s', ctx.event.user_message_alter)
        log_decision(settings.data_dir, 'normal', group=group_id, message_id=message_id, outcome='to-claude',
                     trigger=trigger, prompt_chars=len(ctx.event.user_message_alter))

    async def _on_responded(self, ctx: context.EventContext, settings: Settings, group_id: str) -> None:
        """回复挂载点：错误话术 / <pass/> / <say> / 过滤 / 记录。"""
        event = ctx.event
        raw = str(event.response_text or '')
        buffer = self._get_buffer(settings.data_dir)
        pending = self._pending.pop((group_id, str(event.sender_id)), None)
        trigger = pending[0] if pending else None
        logger.debug('%s 模型原始输出：%s', group_id, preview(raw, 200))

        error_kind = extract_error(raw)
        if error_kind is not None:
            return await self._reply_error(ctx, settings, buffer, group_id, str(event.sender_id), trigger, error_kind)

        if not settings.can_reply or buffer is None or PASS_TOKEN in raw.lower():
            if PASS_TOKEN in raw.lower():
                logger.info('%s 模型选择不回复', group_id)
            else:
                logger.debug('%s 当前不回复，丢弃模型输出', group_id)
            log_decision(settings.data_dir, 'responded', group=group_id, outcome='silent', text=raw)
            return _block(ctx)

        said = extract_say(raw)
        if said is None:
            # 没按格式输出（可能把思考过程混进来了）：宁可不说
            logger.warning('%s 模型的输出没有 <say> 标签，已丢弃（如果经常出现，检查人设里的输出协议）：%s',
                           group_id, preview(raw, 80))
            log_decision(settings.data_dir, 'responded', group=group_id, outcome='no-say-tag', text=raw)
            return _block(ctx)
        parsed = parse_reply(said)
        text = clean_reply(parsed.text, max_chars=settings.max_chars) or ''
        faces = parsed.faces if settings.qq_faces_enabled else []
        sticker = self._pick_sticker(settings, buffer, group_id, parsed.stickers)
        if not (text or faces or sticker):
            logger.info('%s 回复过滤后是空的，不发：%s', group_id, preview(said, 80))
            return _block(ctx)
        if contains_email(text):
            # 上下文里会被 Claude Code 自动注入主人的账号邮箱：任何邮箱都不发
            logger.warning('%s 回复里有邮箱地址，整条不发', group_id)
            log_decision(settings.data_dir, 'responded', group=group_id, outcome='blocked-email', text='[含邮箱，已拦截]')
            return _block(ctx)

        components: list[platform_message.MessageComponent] = []
        if text:
            components.append(platform_message.Plain(text=text))
        components.extend(platform_message.Face(face_id=fid, face_name=face_name(fid)) for fid in faces)
        if sticker:
            components.append(platform_message.Image(base64=base64.b64encode(sticker[1]).decode('ascii')))
        await self._deliver(ctx, settings, platform_message.MessageChain(components),
                            quote=self._should_quote(group_id, trigger))

        recorded = format_chain(components)
        if sticker:
            recorded = recorded.replace('[图片]', f'[表情包：{sticker[0]}]')
            buffer.record_media(group_id, 'sticker', sticker[0])
        reply_to = buffer.name_of(group_id, event.sender_id) or str(event.sender_id)
        self._record_self(settings, buffer, group_id, recorded, reply_to)
        logger.info('已回复 %s（%s%s）：%s', group_id, '引用原消息' if self._should_quote(group_id, trigger) else '不引用',
                    f'，表情包「{sticker[0]}」' if sticker else '', preview(recorded, 60))
        log_decision(settings.data_dir, 'responded', group=group_id, outcome='sent', text=recorded,
                     faces=len(faces), sticker=sticker[0] if sticker else None)
