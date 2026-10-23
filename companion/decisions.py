"""判断记录：每个挂载点对白名单群消息做了什么决定、为什么，写成一行 JSON。

只在本机 data-dir 里，文件超过 5 MB 时轮换成 decisions.log.1。只记判断依据，不记消息全文（只记前 40 个字）。
"""

from __future__ import annotations

import json
import pathlib
import threading
import time
import typing

MAX_BYTES = 5 * 1024 * 1024
_lock = threading.Lock()


def log_decision(data_dir: str, hook: str, **fields: typing.Any) -> None:
    if not data_dir:
        return
    try:
        path = pathlib.Path(data_dir) / 'decisions.log'
        record = {'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'hook': hook, **fields}
        if isinstance(record.get('text'), str):
            record['text'] = record['text'][:40]
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > MAX_BYTES:
                path.replace(path.with_suffix('.log.1'))
            with path.open('a', encoding='utf-8') as f:
                f.write(line + '\n')
    except Exception:
        pass  # 记录失败不能影响正常处理
