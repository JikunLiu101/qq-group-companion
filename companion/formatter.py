"""把 LangBot 消息链转成一行纯文字，供本地记录和交给 Claude。

Claude Code Runner 只接收文字：图片、表情、语音等都要在这里转成可读的占位描述。
链接一律替换成 [链接]：Claude 打不开，也能减少提示注入的入口。
"""

from __future__ import annotations

import re
import typing

NameOf = typing.Callable[[str], typing.Optional[str]]

_URL = re.compile(r'https?://\S+', re.IGNORECASE)
_WS = re.compile(r'\s+')
_MFACE_SUMMARY = re.compile(r"""['"]summary['"]\s*:\s*['"]([^'"]*)['"]""")

# QQ 系统表情 id 远小于这个值；表情包（收藏表情）用的是很大的 package id
_STICKER_ID_MIN = 10000
QUOTE_PREVIEW_CHARS = 30


def _get(component: typing.Any, key: str, default: typing.Any = None) -> typing.Any:
    if isinstance(component, dict):
        return component.get(key, default)
    return getattr(component, key, default)


def _person(qq: typing.Any, name_of: NameOf | None) -> str:
    qq = str(qq or '')
    name = name_of(qq) if (name_of and qq) else None
    return name or qq or '某人'


def _face(component: typing.Any) -> str:
    face_type = _get(component, 'face_type', 'face')
    if face_type == 'rps':
        return '[猜拳]'
    if face_type == 'dice':
        return '[骰子]'
    name = str(_get(component, 'face_name', '') or '').strip()
    face_id = int(_get(component, 'face_id', 0) or 0)
    if face_id >= _STICKER_ID_MIN or name.startswith('['):
        label = name.strip('[]').strip()
        if not label or label in ('动画表情', '表情'):
            return '[表情包]'
        return f'[表情包：{label}]'
    return f'[QQ表情：{name}]' if name else '[QQ表情]'


def _unknown(component: typing.Any) -> str:
    text = str(_get(component, 'text', '') or '')
    kind = text.split(':', 1)[0].strip().lower()
    if kind == 'video':
        return '[视频]'
    if kind == 'mface':
        match = _MFACE_SUMMARY.search(text)
        label = match.group(1).strip('[]').strip() if match else ''
        return f'[表情：{label}]' if label else '[表情]'
    if kind == 'poke':
        return '[戳一戳]'
    return '[不支持的消息]'


def _clean_text(text: str) -> str:
    return _URL.sub('[链接]', text)


def addressees(chain: typing.Iterable[typing.Any], *, name_of: NameOf | None = None) -> list[str]:
    """这条消息是对谁说的：被 @ 的人和被回复的人（按出现顺序去重）。"""
    names: list[str] = []
    for component in chain or []:
        kind = _get(component, 'type', '')
        if kind == 'At':
            display = str(_get(component, 'display', '') or '').lstrip('@')
            names.append(display or _person(_get(component, 'target'), name_of))
        elif kind == 'AtAll':
            names.append('全体成员')
        elif kind == 'Quote':
            names.append(_person(_get(component, 'sender_id'), name_of))
    return list(dict.fromkeys(name for name in names if name))


def format_chain(
    chain: typing.Iterable[typing.Any],
    *,
    name_of: NameOf | None = None,
    _nested: bool = False,
) -> str:
    """消息链 → 一行文字。name_of(qq) 用来把 @ 和引用里的 QQ 号换成昵称。"""
    parts: list[str] = []
    for component in chain or []:
        kind = _get(component, 'type', '')
        if kind == 'Source':
            continue
        if kind == 'Plain':
            parts.append(_clean_text(str(_get(component, 'text', '') or '')))
        elif kind == 'At':
            display = str(_get(component, 'display', '') or '').lstrip('@')
            parts.append(f'@{display or _person(_get(component, "target"), name_of)} ')
        elif kind == 'AtAll':
            parts.append('@全体成员 ')
        elif kind == 'Image':
            parts.append('[图片]')
        elif kind == 'Face':
            parts.append(_face(component))
        elif kind == 'Voice':
            parts.append('[语音]')
        elif kind == 'File':
            name = str(_get(component, 'name', '') or '').strip()
            parts.append(f'[文件：{name}]' if name else '[文件]')
        elif kind == 'Forward':
            parts.append('[聊天记录]')
        elif kind == 'Quote':
            if _nested:
                continue
            who = _person(_get(component, 'sender_id'), name_of)
            origin = format_chain(_get(component, 'origin') or [], name_of=name_of, _nested=True)
            if len(origin) > QUOTE_PREVIEW_CHARS:
                origin = origin[:QUOTE_PREVIEW_CHARS] + '…'
            parts.append(f'（回复 {who}：{origin}）' if origin else f'（回复 {who}）')
        elif kind == 'Unknown':
            parts.append(_unknown(component))
        else:
            parts.append('[不支持的消息]')
    text = _WS.sub(' ', ''.join(parts)).strip()
    return text or '[空消息]'
