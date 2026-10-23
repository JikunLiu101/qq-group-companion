"""私聊白名单：只有白名单里的人走插件流程，其他私聊（包括微信）原样放过。"""

import datetime

import langbot_plugin.api.entities.builtin.platform.entities as pent
import langbot_plugin.api.entities.builtin.platform.events as pe
import langbot_plugin.api.entities.builtin.platform.message as m
import langbot_plugin.api.entities.builtin.provider.session as ps
from langbot_plugin.api.entities import context, events

from companion.gate import gate_regexp  # noqa: F401  (保证与群聊同一套暗号)
from tests.test_listener import SECRET, handler, listener, run, sent  # noqa: F401  (pytest fixtures)

OWNER = 900000002
STRANGER = 900000003
WECHAT = 'wxid_abc123'


def _person_event(cls, user, msg_id, parts, with_source=True, **extra):
    friend = pent.Friend(id=user, nickname='店长本人', remark=None)
    head = [m.Source(id=msg_id, time=datetime.datetime.now())] if with_source else []
    chain = m.MessageChain([*head, *parts])
    me = pe.FriendMessage(sender=friend, message_chain=chain, time=datetime.datetime.now().timestamp())
    ev = cls(launcher_type='person', launcher_id=user, sender_id=user, message_event=me, message_chain=chain, **extra)
    return context.EventContext.from_event(ev)


def private_flow(inst, user, msg_id, text):
    """收到 → (LangBot 丢 Source、合并文字) → 交给 Claude 前。返回第二个挂载点的 ctx。"""
    first = _person_event(events.PersonMessageReceived, user, msg_id, [m.Plain(text=text)])
    run(handler(inst, events.PersonMessageReceived)(first))
    if first.is_prevented_default():
        return first, None
    merged = ''.join(c.text for c in first.event.message_chain if c.type == 'Plain')
    second = _person_event(events.PersonNormalMessageReceived, user, msg_id, [m.Plain(text=merged)],
                           with_source=False, text_message='')
    run(handler(inst, events.PersonNormalMessageReceived)(second))
    return first, second


def private_responded(inst, user, text):
    session = ps.Session(launcher_type=ps.LauncherTypes('person'), launcher_id=user)
    ev = events.NormalMessageResponded(
        launcher_type='person', launcher_id=user, sender_id=user, session=session,
        prefix='', response_text=text, finish_reason='stop', funcs_called=[],
    )
    ctx = context.EventContext.from_event(ev)
    run(handler(inst, events.NormalMessageResponded)(ctx))
    return ctx


def _enable(inst):
    inst.config.update({'reply-mode': 'mention', 'private-whitelist': str(OWNER), 'owner-qq': str(OWNER)})


def test_whitelisted_private_chat_reaches_claude(listener, sent):
    _enable(listener)
    first, second = private_flow(listener, OWNER, 500, '在吗')
    assert not first.is_prevented_default() and second is not None and not second.is_prevented_default()
    prompt = second.event.user_message_alter
    assert '与主人的私聊' in prompt and '主人在私聊你' in prompt and '在吗' in prompt and 'QGC' not in prompt
    ctx = private_responded(listener, OWNER, '<say>哼，在。</say>')
    assert str(ctx.event.reply_message_chain) == '哼，在'
    assert sent[-1] == ('哼，在', False)  # 私聊不引用
    assert listener._buffer.last_self('private-900000002').text == '哼，在'


def test_other_private_chats_are_untouched(listener):
    _enable(listener)
    for user in (STRANGER, WECHAT):
        first, second = private_flow(listener, user, 510, '在吗')
        assert not first.is_prevented_default() and not second.is_prevented_default()
        assert second.event.user_message_alter is None  # 没被插件改写
        ctx = private_responded(listener, user, '这是微信那边的正常回复')
        assert not ctx.is_prevented_default() and ctx.event.reply_message_chain is None
    assert listener._buffer is None or listener._buffer.count(str(STRANGER)) == 0


def test_private_without_marker_is_blocked(listener):
    _enable(listener)
    ctx = _person_event(events.PersonNormalMessageReceived, OWNER, 520, [m.Plain(text='排队追加的消息')],
                        with_source=False, text_message='')
    run(handler(inst := listener, events.PersonNormalMessageReceived)(ctx))
    assert ctx.is_prevented_default() and inst is listener


def test_private_does_not_use_strong_quota(listener):
    _enable(listener)
    listener.config['user-daily-strong-limit'] = 1
    for i in range(3):
        _, second = private_flow(listener, OWNER, 530 + i, f'第 {i} 条')
        assert second is not None and not second.is_prevented_default()


def test_private_error_gets_preset_reply(listener, sent):
    _enable(listener)
    private_flow(listener, OWNER, 540, '在吗')
    private_responded(listener, OWNER, '<qgc-error kind="limit"/>')
    assert len(sent) == 1 and sent[0][1] is False


def test_private_commands_blocked_only_for_whitelist(listener):
    _enable(listener)
    for user, blocked in ((OWNER, True), (WECHAT, False)):
        ctx = _person_event(events.PersonCommandSent, user, 550, [m.Plain(text='!help')],
                            command='help', params=[], text_message='!help', is_admin=False)
        run(handler(listener, events.PersonCommandSent)(ctx))
        assert ctx.is_prevented_default() is blocked
