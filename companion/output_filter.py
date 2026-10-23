"""发出前的最后一道过滤。返回 None 表示这次不发。"""

from __future__ import annotations

import re

PASS_TOKEN = '<pass/>'
DEFAULT_MAX_CHARS = 300

_SAY = re.compile(r'<say>(.*?)</say>', re.DOTALL | re.IGNORECASE)
EMAIL = re.compile(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}')


def extract_say(raw: str | None) -> str | None:
    """只取 <say>…</say> 里的内容（多段按换行拼接）；没有这个标签返回 None。

    模型偶尔会把"思考过程"写在正文前面；要求它把要发的话放进 <say>，标签外的一律丢掉。
    """
    if not raw:
        return None
    parts = [p.strip() for p in _SAY.findall(raw)]
    parts = [p for p in parts if p]
    return '\n'.join(parts) if parts else None


_ERROR = re.compile(r'<qgc-error\s+kind="([a-z]+)"\s*/>', re.IGNORECASE)
ERROR_KINDS = ('limit', 'refused', 'other')


def extract_error(raw: str | None) -> str | None:
    """包装脚本（例如 claude-usage-guard）在 claude 出错或额度锁定时输出 <qgc-error kind="..."/>；返回 kind，不是错误返回 None。"""
    if not raw:
        return None
    match = _ERROR.search(raw)
    if not match:
        return None
    kind = match.group(1).lower()
    return kind if kind in ERROR_KINDS else 'other'


def contains_email(text: str | None) -> bool:
    return bool(text) and bool(EMAIL.search(text))

_URL = re.compile(r'(https?://|www\.)\S+', re.IGNORECASE)
_CODE_FENCE = re.compile(r'```.*?```', re.DOTALL)
_MD_MARKS = re.compile(r'(\*\*|__|`|^#{1,6}\s*|^>\s?)', re.MULTILINE)
_BLANK_LINES = re.compile(r'\n{3,}')


_FINAL_PERIOD = re.compile(r'(?<![.。…])[。.]\s*$')  # 只去单个句号，省略号不动


def drop_final_period(text: str) -> str:
    """去掉末尾的句号（"……"和"..."保留）：群里没人打字带句号。"""
    return _FINAL_PERIOD.sub('', text.rstrip()) if text else text


def clean_reply(text: str | None, *, max_chars: int = DEFAULT_MAX_CHARS) -> str | None:
    if text is None:
        return None
    text = str(text).strip()
    # 只要出现 <pass/>（哪怕前后带了别的字），就当作选择沉默：宁可少说，不可乱说
    if not text or PASS_TOKEN in text.lower():
        return None
    text = _CODE_FENCE.sub('', text)
    text = _URL.sub('', text)
    text = _MD_MARKS.sub('', text)
    text = _BLANK_LINES.sub('\n\n', text).strip()
    if not text:
        return None
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + '…'
    return drop_final_period(text) or None
