"""放行规则：谁在叫"我"、叫得有多明确、还有没有额度。

强点名（@我、回复我的消息）：每个人每天有固定次数，不限频率；超额后由插件回一句预设话术。
弱点名（只是提到名字）：容易误判，消息明显是对别人说的就不算；每个群每分钟有次数上限。
所有回复再受每群、全部群的每日总量兜底。
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import re
import time
import typing

from companion.buffer import MessageBuffer

TRIGGER_AT = 'at'
TRIGGER_REPLY = 'reply'
TRIGGER_NAME = 'name'
TRIGGER_OWNER = 'owner'
TRIGGER_PRIVATE = 'private'

STRONG = 'strong'
WEAK = 'weak'
STRENGTH = {TRIGGER_AT: STRONG, TRIGGER_REPLY: STRONG, TRIGGER_NAME: WEAK, TRIGGER_OWNER: WEAK, TRIGGER_PRIVATE: STRONG}

TRIGGER_LABELS = {
    TRIGGER_AT: '有人 @了你（明确在叫你）',
    TRIGGER_REPLY: '有人回复了你的消息（明确在跟你说话）',
    TRIGGER_NAME: '有人提到了你的名字（不一定是在跟你说话；拿不准就 <pass/>）',
    TRIGGER_PRIVATE: '主人在私聊你（只有主人能私聊你，常用来检查你是否正常；照常回就行）',
    TRIGGER_OWNER: '有人提到了你的主人（不一定在说主人，主人的称呼也可能只是普通的词；需要时可以替主人说话，拿不准就 <pass/>）',
}

# 放行原因（写进 triggers 表的 outcome）
PASSED = 'passed'
OVER_USER_LIMIT = 'user-limit'
NOTICE_SENT = 'notice'
WEAK_RATE = 'weak-rate'
GROUP_DAILY = 'group-daily-limit'
TOTAL_DAILY = 'total-daily-limit'


def _get(component: typing.Any, key: str, default: typing.Any = None) -> typing.Any:
    if isinstance(component, dict):
        return component.get(key, default)
    return getattr(component, key, default)


def parse_aliases(raw: str | None) -> list[str]:
    """名字和别名，逗号分隔；单个字符的别名太容易误触，忽略。"""
    if not raw:
        return []
    items = [item.strip() for item in re.split(r'[,，;；]+', str(raw))]
    return [item for item in items if len(item) >= 2]


def _alias_pattern(alias: str) -> re.Pattern[str]:
    # 前后不能紧挨字母数字：避免 "69" 命中 "1969"
    return re.compile(rf'(?<![0-9A-Za-z]){re.escape(alias)}(?![0-9A-Za-z])', re.IGNORECASE)


def detect_trigger(
    chain: typing.Iterable[typing.Any],
    self_qq: str,
    aliases: list[str],
    *,
    owner_aliases: typing.Sequence[str] = (),
    sender_id: object = None,
    owner_qq: str = '',
) -> str | None:
    """这条消息是否在叫"我"。返回 at / reply / name / owner，或 None。

    提到名字但同时 @了别人、或在回复别人的消息（而且没有 @我）时，视为在对别人说话，不算点名。
    提到主人的绰号算 owner（弱点名）；主人自己说的不算。
    """
    self_qq = str(self_qq or '')
    text_parts: list[str] = []
    addressed_to_others = False
    for component in chain or []:
        kind = _get(component, 'type', '')
        if kind == 'At':
            if self_qq and str(_get(component, 'target', '')) == self_qq:
                return TRIGGER_AT
            addressed_to_others = True
        elif kind == 'AtAll':
            addressed_to_others = True
        elif kind == 'Quote':
            if self_qq and str(_get(component, 'sender_id', '')) == self_qq:
                return TRIGGER_REPLY
            addressed_to_others = True
        elif kind == 'Plain':
            text_parts.append(str(_get(component, 'text', '') or ''))
    if addressed_to_others:
        return None
    text = ''.join(text_parts)
    if any(_alias_pattern(alias).search(text) for alias in aliases):
        return TRIGGER_NAME
    from_owner = bool(owner_qq) and str(sender_id) == str(owner_qq)
    if not from_owner and any(_alias_pattern(alias).search(text) for alias in owner_aliases):
        return TRIGGER_OWNER
    return None


@dataclasses.dataclass(frozen=True)
class Limits:
    per_user_strong_daily: int = 20
    weak_per_minute: int = 3
    per_group_daily: int = 100
    total_daily: int = 300

    @classmethod
    def from_config(cls, config: dict[str, typing.Any]) -> Limits:
        def number(key: str, default: int) -> int:
            try:
                value = int(config.get(key, default))
            except (TypeError, ValueError):
                return default
            return value if value >= 0 else default

        return cls(
            per_user_strong_daily=number('user-daily-strong-limit', cls.per_user_strong_daily),
            weak_per_minute=number('weak-per-minute', cls.weak_per_minute),
            per_group_daily=number('daily-limit-per-group', cls.per_group_daily),
            total_daily=number('daily-limit-total', cls.total_daily),
        )


def start_of_today(now: float | None = None) -> float:
    local = time.localtime(now if now is not None else time.time())
    return time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1))


def check_limits(
    buffer: MessageBuffer,
    group_id: object,
    user_id: object,
    trigger: str,
    limits: Limits,
    now: float | None = None,
) -> str | None:
    """超出限制时返回原因；没超出返回 None。"""
    now = now if now is not None else time.time()
    today = start_of_today(now)
    if STRENGTH[trigger] == STRONG:
        used = buffer.count_triggers(since=today, outcome=PASSED, user_id=user_id, strength=STRONG)
        if used >= limits.per_user_strong_daily:
            return OVER_USER_LIMIT
    else:
        recent = buffer.count_triggers(since=now - 60, outcome=PASSED, group_id=group_id, strength=WEAK)
        if recent >= limits.weak_per_minute:
            return WEAK_RATE
    if buffer.count_self_since(today, group_id) >= limits.per_group_daily:
        return GROUP_DAILY
    if buffer.count_self_since(today) >= limits.total_daily:
        return TOTAL_DAILY
    return None


def notice_already_sent(buffer: MessageBuffer, group_id: object, user_id: object, now: float | None = None) -> bool:
    """今天是否已经给这个人发过"额度用完"的话术（每人每天只发一次）。"""
    return buffer.count_triggers(
        since=start_of_today(now), outcome=NOTICE_SENT, group_id=group_id, user_id=user_id
    ) > 0


# ---------- 暗号：插件放行的消息才能通过流水线的群规则 ----------
#
# LangBot 的 @ 规则会删掉消息里的 @，排队中的消息还会绕过插件直接尝试"追加"进正在运行的 Claude。
# 所以插件放行时在消息开头插入一段暗号（带触发类型），流水线群规则只用正则匹配这个暗号：
# - 没经过插件的消息没有暗号，过不了群规则（包括排队追加那条路）；
# - 第二个挂载点从暗号里读出触发类型，不再依赖已经被删掉的 @。
# - 暗号开头是由密钥算出的固定标签（流水线正则只匹配它），群友不知道密钥就猜不出，排队追加那条路也进不来；
# - 后面带上触发类型和消息 ID，再加一段对它们的 HMAC，第二个挂载点逐条校验。
#   消息 ID 必须写进暗号里：LangBot 在两个挂载点之间会丢掉消息链里的 Source、并把文字段合并成一段，
#   第二个挂载点拿不到原来的消息 ID（2026-10-23 线上实测）。

MARKER_PREFIX = '⁣QGC:'
MARKER_SUFFIX = '⁣'
_MARKER = re.compile(r'⁣QGC:([0-9a-f]{12}):([a-z]+):([0-9A-Za-z_.\-]{1,40}):([0-9a-f]{16})⁣')


class Marker(typing.NamedTuple):
    trigger: str
    message_id: str


def _hmac(secret: str, text: str, length: int) -> str:
    return hmac.new(secret.encode(), text.encode(), hashlib.sha256).hexdigest()[:length]


def gate_tag(secret: str) -> str:
    return _hmac(secret, 'gate-tag', 12)


def gate_regexp(secret: str) -> str:
    """流水线 group-respond-rules.regexp 里要填的正则。"""
    return f'^⁣QGC:{gate_tag(secret)}:'


_SAFE_ID = re.compile(r'[^0-9A-Za-z_.\-]')


def _safe_id(message_id: object) -> str:
    return _SAFE_ID.sub('_', str(message_id))[:40] or '_'


def make_marker(secret: str, trigger: str, message_id: object) -> str:
    mid = _safe_id(message_id)
    digest = _hmac(secret, f'{trigger}:{mid}', 16)
    return f'{MARKER_PREFIX}{gate_tag(secret)}:{trigger}:{mid}:{digest}{MARKER_SUFFIX}'


def strip_marker(text: str) -> str:
    return _MARKER.sub('', text or '')


def read_marker(text: str, secret: str) -> Marker | None:
    """从消息文字里取出并校验暗号，返回 (触发类型, 消息 ID)；没有或不对返回 None。"""
    if not secret:
        return None
    match = _MARKER.search(text or '')
    if not match:
        return None
    tag, trigger, mid, digest = match.groups()
    if trigger not in STRENGTH:
        return None
    if not (hmac.compare_digest(tag, gate_tag(secret)) and hmac.compare_digest(digest, _hmac(secret, f'{trigger}:{mid}', 16))):
        return None
    return Marker(trigger, mid)
