"""本地表情库与回复里的表情标记。

人设在回复里写：
- [表情:狗头]   → 本地表情库里文件名为"狗头"的图片
- [QQ表情:捂脸] → QQ 系统表情
"""

from __future__ import annotations

import pathlib
import re
import typing

from companion.faces import face_id

STICKER_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.webp'}
MAX_STICKER_BYTES = 2 * 1024 * 1024
MAX_FACES_PER_REPLY = 2

_TOKEN = re.compile(r'\[\s*(QQ表情|表情)\s*[:：]\s*([^\[\]\n]{1,20}?)\s*\]', re.IGNORECASE)


class StickerLibrary:
    """扫描表情文件夹：文件名（不含扩展名）就是标签。目录有变化时自动重新扫描。"""

    def __init__(self, directory: str | pathlib.Path):
        self.directory = pathlib.Path(directory)
        self._signature: tuple | None = None
        self._tags: dict[str, pathlib.Path] = {}

    def _scan_signature(self) -> tuple:
        if not self.directory.is_dir():
            return ()
        return tuple(sorted((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in self.directory.iterdir() if p.is_file()))

    def _refresh(self) -> None:
        signature = self._scan_signature()
        if signature == self._signature:
            return
        tags: dict[str, pathlib.Path] = {}
        if self.directory.is_dir():
            for path in sorted(self.directory.iterdir()):
                if path.is_file() and path.suffix.lower() in STICKER_EXTS and path.stat().st_size <= MAX_STICKER_BYTES:
                    tag = path.stem.strip()
                    if tag and tag not in tags:
                        tags[tag] = path
        self._tags = tags
        self._signature = signature

    def tags(self) -> list[str]:
        self._refresh()
        return list(self._tags)

    def path_of(self, tag: str) -> pathlib.Path | None:
        self._refresh()
        return self._tags.get(str(tag).strip())


class ParsedReply(typing.NamedTuple):
    text: str              # 去掉所有表情标记后的文字
    stickers: list[str]    # [表情:xxx] 的标签，按出现顺序
    faces: list[int]       # [QQ表情:xxx] 中认得的表情 id，最多 MAX_FACES_PER_REPLY 个


def parse_reply(text: str) -> ParsedReply:
    stickers: list[str] = []
    faces: list[int] = []

    def replace(match: re.Match[str]) -> str:
        kind, name = match.group(1), match.group(2).strip()
        if kind.upper() == 'QQ表情':
            fid = face_id(name)
            if fid is not None and len(faces) < MAX_FACES_PER_REPLY:
                faces.append(fid)
        else:
            stickers.append(name)
        return ''

    stripped = _TOKEN.sub(replace, text or '')
    return ParsedReply(text=stripped, stickers=stickers, faces=faces)
