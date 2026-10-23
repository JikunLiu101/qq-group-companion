"""把本地缓冲里的群聊记录整理成交给 Claude 的输入。

每次回复都是一个新的 Claude 会话（不复用），人设走系统提示，群聊上下文全部由这里提供：
最近一段时间（默认 2 小时）内、最多 40 条消息，每条注明是对谁说的。
这样输入大小固定、不会随时间膨胀，人设改了也立即生效。
"""

from __future__ import annotations

import time
import typing

from companion.buffer import MessageBuffer, Record
from companion.gate import TRIGGER_LABELS

DEFAULT_MAX_MESSAGES = 40
DEFAULT_WINDOW_SECONDS = 2 * 3600
MAX_LINE_CHARS = 200       # 之前的消息单条最多这么长，超出截断
MAX_STICKER_TAGS = 60      # 标签太多会挤占输入，只列前 60 个


def select_context(
    buffer: MessageBuffer,
    group_id: object,
    now: float | None = None,
    *,
    max_messages: int = DEFAULT_MAX_MESSAGES,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
) -> list[Record]:
    now = now if now is not None else time.time()
    return buffer.since(group_id, now - window_seconds, limit=max_messages)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + '…'


def _line(record: Record, *, clip: bool = True, owner_qq: str = '') -> str:
    stamp = time.strftime('%H:%M', time.localtime(record.ts))
    who = '你' if record.is_self else (record.sender_name or record.sender_id)
    if owner_qq and not record.is_self and str(record.sender_id) == str(owner_qq):
        who = f'{who}（主人）'
    target = f' → {record.reply_to}' if record.reply_to else ''
    text = _clip(record.text, MAX_LINE_CHARS) if clip else record.text
    return f'{stamp} {who}{target}：{text}'


def build_prompt(
    *,
    group_name: str,
    trigger: str,
    context: list[Record],
    current: Record,
    quoted: str = '',
    image_paths: typing.Sequence[str] = (),
    sticker_tags: typing.Sequence[str] = (),
    owner_qq: str = '',
    self_nickname: str = '',
) -> str:
    """生成一段纯文字输入。current 是触发这次回复的那条消息。

    quoted：current 回复的那条原消息（完整文字，例如 "白锦鲤：发育差强行打到5层……"）。
    image_paths：本条（含引用）附带、已保存到本地的图片，Claude 可用 Read 查看。
    sticker_tags：本地表情库里可用的标签。
    """
    earlier = [r for r in context if not (r.message_id == current.message_id and r.group_id == current.group_id)]
    lines = [
        f'[群：{group_name or current.group_id}]',
        f'[情况：{TRIGGER_LABELS.get(trigger, trigger)}]',
        *([f'[你在本群的昵称：{self_nickname}]'] if self_nickname else []),
        '[格式：时间 发言人 → 对谁说：内容；"你"是你自己；"（主人）"是你的主人]',
    ]
    if earlier:
        lines.append('[之前的群聊]')
        lines.extend(_line(r, owner_qq=owner_qq) for r in earlier)
    lines.append('[刚刚这条]')
    lines.append(_line(current, clip=False, owner_qq=owner_qq))
    if quoted:
        lines.append(f'[它回复的原消息] {quoted}')
    if image_paths:
        lines.append('[本条附带的图片，需要时用 Read 查看]')
        lines.extend(f'- {path}' for path in image_paths)
    if sticker_tags:
        lines.append(f'[可用表情包：{"、".join(sticker_tags[:MAX_STICKER_TAGS])}]')
    lines.append('[按你的设定回一句：只把要发到群里的话写在 <say> 和 </say> 之间，标签外不要写任何东西；不想说话就只输出 <pass/>]')
    return '\n'.join(lines)
