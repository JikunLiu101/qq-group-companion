"""看图：把放行消息里的图片下载到本地 inbox，供 Claude 用 Read 查看。

安全约束：
- 只从 QQ 图片服务器（腾讯）的域名下载，不跟随跳转，防止消息里塞进任意地址；
- 按文件头确认确实是图片，限制大小；
- 文件名随机生成，不使用消息里给的名字。
"""

from __future__ import annotations

import base64
import binascii
import datetime
import pathlib
import secrets
import time
import typing
import urllib.parse

import httpx

ALLOWED_HOST_SUFFIXES = ('.qq.com', '.qpic.cn', '.qq.com.cn')
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_IMAGES = 3
DOWNLOAD_TIMEOUT = 10.0

_SIGNATURES: list[tuple[bytes, str]] = [
    (b'\xff\xd8\xff', 'jpg'),
    (b'\x89PNG\r\n\x1a\n', 'png'),
    (b'GIF87a', 'gif'),
    (b'GIF89a', 'gif'),
    (b'BM', 'bmp'),
]
IMAGE_EXTS = {'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp'}


def sniff_image(data: bytes) -> str | None:
    """看文件头判断图片格式，不是图片返回 None。"""
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'webp'
    for signature, ext in _SIGNATURES:
        if data.startswith(signature):
            return ext
    return None


def host_allowed(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or '').lower()
    return parsed.scheme in ('http', 'https') and any(
        host == suffix.lstrip('.') or host.endswith(suffix) for suffix in ALLOWED_HOST_SUFFIXES
    )


def _get(component: typing.Any, key: str, default: typing.Any = None) -> typing.Any:
    if isinstance(component, dict):
        return component.get(key, default)
    return getattr(component, key, default)


def collect_images(chain: typing.Iterable[typing.Any], limit: int = DEFAULT_MAX_IMAGES) -> list[typing.Any]:
    """本条消息里的图片，加上它引用的那条消息里的图片，最多 limit 张。"""
    found: list[typing.Any] = []
    for component in chain or []:
        kind = _get(component, 'type', '')
        if kind == 'Image':
            found.append(component)
        elif kind == 'Quote':
            found.extend(c for c in (_get(component, 'origin') or []) if _get(c, 'type', '') == 'Image')
    return found[:limit]


def _decode_base64(value: str) -> bytes | None:
    if value.startswith('base64://'):
        value = value[len('base64://'):]
    elif value.startswith('data:') and ',' in value:
        value = value.split(',', 1)[1]
    try:
        return base64.b64decode(value, validate=False)
    except (binascii.Error, ValueError):
        return None


async def _download(client: httpx.AsyncClient, url: str, max_bytes: int) -> bytes | None:
    if not host_allowed(url):
        return None
    try:
        async with client.stream('GET', url) as response:
            if response.status_code != 200:
                return None
            declared = response.headers.get('content-length')
            if declared and declared.isdigit() and int(declared) > max_bytes:
                return None
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    return None
                chunks.append(chunk)
            return b''.join(chunks)
    except httpx.HTTPError:
        return None


async def save_images(
    images: list[typing.Any],
    inbox_dir: str | pathlib.Path,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    client: httpx.AsyncClient | None = None,
) -> list[pathlib.Path]:
    """下载/解码图片并保存到 inbox/<日期>/<随机名>.<ext>，返回成功保存的路径。"""
    if not images:
        return []
    day_dir = pathlib.Path(inbox_dir) / datetime.date.today().isoformat()
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT, follow_redirects=False, trust_env=False)
    saved: list[pathlib.Path] = []
    try:
        for image in images:
            data: bytes | None = None
            b64 = _get(image, 'base64')
            if b64:
                data = _decode_base64(str(b64))
            elif _get(image, 'url'):
                data = await _download(client, str(_get(image, 'url')), max_bytes)
            if not data or len(data) > max_bytes:
                continue
            ext = sniff_image(data)
            if ext is None:
                continue
            day_dir.mkdir(parents=True, exist_ok=True)
            path = day_dir / f'{secrets.token_hex(8)}.{ext}'
            path.write_bytes(data)
            saved.append(path)
    finally:
        if owns_client:
            await client.aclose()
    return saved


def purge_inbox(inbox_dir: str | pathlib.Path, retention_days: int, now: float | None = None) -> int:
    """删除 inbox 里超过保留期的图片和空日期目录，返回删除的文件数。"""
    root = pathlib.Path(inbox_dir)
    if not root.is_dir():
        return 0
    cutoff = (now if now is not None else time.time()) - retention_days * 86400
    removed = 0
    for path in root.rglob('*'):
        if path.is_file() and path.suffix.lstrip('.').lower() in IMAGE_EXTS and path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    for directory in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True):
        try:
            directory.rmdir()  # 只会删掉空目录
        except OSError:
            pass
    return removed
