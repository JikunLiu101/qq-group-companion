"""插件日志：所有模块的 logger 都挂在 qq_group_companion 下，级别由配置项 log-level 控制。

日志写到插件进程的 stderr，LangBot 会显示在插件详情页。级别约定：
- DEBUG：每条消息的判断过程（排查时打开）
- INFO：放行、回复、限额生效、配置变化等正常事件
- WARNING：可以自动恢复的问题（查询失败、模型没按格式输出、发送失败退回等）
- ERROR：需要人处理的问题（配置缺失或错误、意外异常）
"""

from __future__ import annotations

import logging

ROOT = 'qq_group_companion'
LEVELS = ('DEBUG', 'INFO', 'WARNING', 'ERROR')


def get_logger(module: str) -> logging.Logger:
    return logging.getLogger(f'{ROOT}.{module.rsplit(".", 1)[-1]}')


def apply_level(level: str) -> str:
    level = str(level or 'INFO').upper()
    if level not in LEVELS:
        level = 'INFO'
    logging.getLogger(ROOT).setLevel(level)
    return level


def preview(text: object, limit: int = 30) -> str:
    """日志里只放消息的前几十个字。"""
    s = str(text or '').replace('\n', ' ')
    return s if len(s) <= limit else s[:limit] + '…'
