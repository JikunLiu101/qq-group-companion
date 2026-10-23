"""日志级别约定：需要人处理的才是 ERROR。"""

import logging

import langbot_plugin.api.entities.builtin.platform.message as m
import pytest

from tests.test_listener import GROUP, SELF, full_flow, listener, responded, run, sent  # noqa: F401


@pytest.fixture(autouse=True)
def fresh_report_state():
    """配置检查结果按进程缓存：每个测试从"还没检查过"开始。"""
    import components.event_listener.companion as mod
    mod._reported_config['key'] = None
    yield


def records(caplog, level):
    return [r.getMessage() for r in caplog.records if r.levelno == level and r.name.startswith('qq_group_companion')]


def test_missing_config_is_error(listener, caplog):
    caplog.set_level(logging.INFO, logger='qq_group_companion')
    listener.config.update({'data-dir': '', 'reply-mode': 'mention', 'gate-secret': 'short', 'self-qq': ''})
    listener._settings()
    errors = records(caplog, logging.ERROR)
    assert any('数据目录' in e for e in errors)
    assert any('机器人 QQ 号' in e for e in errors)
    assert any('放行暗号密钥' in e and '正则' in e for e in errors)  # 给出一个随机密钥和对应正则


def test_config_reported_once_and_regex_logged(listener, caplog):
    caplog.set_level(logging.INFO, logger='qq_group_companion')
    listener.config['reply-mode'] = 'mention'
    listener._settings()
    listener._settings()
    infos = records(caplog, logging.INFO)
    assert sum('配置已加载' in i for i in infos) == 1  # 配置没变就不重复汇报
    assert any('群响应规则' in i and 'QGC' in i for i in infos)
    assert not records(caplog, logging.ERROR)


def test_flow_logs_pass_and_reply_at_info(listener, caplog, sent):
    caplog.set_level(logging.INFO, logger='qq_group_companion')
    listener.config['reply-mode'] = 'mention'
    assert full_flow(listener, GROUP, 700, m.At(target=SELF), m.Plain(text='在吗')) is not None
    responded(listener, GROUP, '在')
    infos = records(caplog, logging.INFO)
    assert any(i.startswith('放行') for i in infos)
    assert any(i.startswith('交给模型') for i in infos)
    assert any(i.startswith('已回复') for i in infos)


def test_no_say_tag_is_warning(listener, caplog):
    caplog.set_level(logging.INFO, logger='qq_group_companion')
    listener.config['reply-mode'] = 'mention'
    responded(listener, GROUP, '我想想……回一句', raw=True)
    assert any('<say>' in w for w in records(caplog, logging.WARNING))


def test_unexpected_exception_is_error_and_blocks(listener, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger='qq_group_companion')
    listener.config['reply-mode'] = 'mention'

    def boom(*args, **kwargs):
        raise RuntimeError('boom')

    monkeypatch.setattr('components.event_listener.companion.extract_say', boom)
    ctx = responded(listener, GROUP, '<say>hi</say>', raw=True)
    assert ctx.is_prevented_default()  # 出错时宁可不发
    assert any('处理模型回复时出错' in e for e in records(caplog, logging.ERROR))


def test_log_level_setting(listener, caplog):
    listener.config['log-level'] = 'DEBUG'
    listener._settings()
    assert logging.getLogger('qq_group_companion').level == logging.DEBUG
    listener.config['log-level'] = 'nonsense'
    listener._settings()
    assert logging.getLogger('qq_group_companion').level == logging.INFO
