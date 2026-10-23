"""按 LangBot 运行时的方式加载事件监听，走完整个流程。

会模拟 LangBot 在两个挂载点之间做的事：群规则会删掉消息里的 @。
"""

import asyncio
import base64
import datetime
import importlib
import re

import pytest

import langbot_plugin.api.entities.builtin.platform.entities as pent
import langbot_plugin.api.entities.builtin.platform.events as pe
import langbot_plugin.api.entities.builtin.platform.message as m
import langbot_plugin.api.entities.builtin.provider.session as ps
from langbot_plugin.api.entities import context, events

from companion.gate import gate_regexp

SELF = '900000001'
GROUP = 100000001
OTHER_GROUP = 100000002
SECRET = 'k' * 24
PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 16


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def sent(monkeypatch):
    """记录插件用 ctx.reply 主动发出的消息。"""
    messages = []

    async def fake_reply(self, message_chain, quote_origin=False):
        messages.append((str(message_chain), quote_origin))
        if hasattr(self.event, 'reply_message_chain'):
            self.event.reply_message_chain = message_chain  # 方便断言发了什么

    monkeypatch.setattr(context.EventContext, 'reply', fake_reply)
    return messages


@pytest.fixture
def listener(tmp_path, sent):
    mod = importlib.import_module('components.event_listener.companion')
    inst = mod.CompanionEventListener()
    inst.config = {
        'group-whitelist': str(GROUP), 'data-dir': str(tmp_path / 'data'), 'reply-mode': 'listen',
        'self-qq': SELF, 'persona-name': '大小姐', 'persona-aliases': '', 'gate-secret': SECRET,
        'reply-delay-min-seconds': 0, 'reply-delay-max-seconds': 0,
    }
    inst.get_plugin_config = lambda: inst.config
    run(inst.initialize())
    yield inst
    if inst._buffer:
        inst._buffer.close()


def handler(inst, event_type):
    return inst.registered_handlers[event_type][0]


def _group_event(cls, gid, msg_id, parts, sender=(900000003, '阿明'), with_source=True, **extra):
    grp = pent.Group(id=gid, name='测试群', permission=pent.Permission.Member)
    snd = pent.GroupMember(id=sender[0], member_name=sender[1], permission=pent.Permission.Member, group=grp)
    head = [m.Source(id=msg_id, time=datetime.datetime.now())] if with_source else []
    chain = m.MessageChain([*head, *parts])
    me = pe.GroupMessage(sender=snd, message_chain=chain, time=datetime.datetime.now().timestamp())
    ev = cls(launcher_type='group', launcher_id=gid, sender_id=sender[0], message_event=me, message_chain=chain, **extra)
    return context.EventContext.from_event(ev)


def received(inst, gid, msg_id, *parts, sender=(900000003, '阿明')):
    ctx = _group_event(events.GroupMessageReceived, gid, msg_id, parts, sender)
    run(handler(inst, events.GroupMessageReceived)(ctx))
    return ctx


def rule_stage_passes(inst, received_ctx) -> bool:
    """模拟流水线群规则：只认暗号（regexp 规则匹配 str(message_chain)）。"""
    return bool(re.match(gate_regexp(SECRET), str(received_ctx.event.message_chain)))


def normal_from(inst, received_ctx, gid, msg_id, sender=(900000003, '阿明')):
    """模拟 LangBot 在两个挂载点之间做的事（2026-10-23 线上实测）：
    删掉 @、丢掉 Source、把相邻的文字段合并成一段，再触发第二个挂载点。"""
    parts = []
    for c in received_ctx.event.message_chain:
        if c.type in ('Source', 'At'):
            continue
        if c.type == 'Plain' and parts and parts[-1].type == 'Plain':
            parts[-1] = m.Plain(text=parts[-1].text + c.text)
        else:
            parts.append(c)
    ctx = _group_event(events.GroupNormalMessageReceived, gid, msg_id, parts, sender, text_message='', with_source=False)
    run(handler(inst, events.GroupNormalMessageReceived)(ctx))
    return ctx


def responded(inst, gid, text, launcher_type='group', sender_id=900000003, raw=False):
    """模拟 Claude 的回复；默认按约定包在 <say> 里（<pass/> 和 raw=True 时原样）。"""
    if not raw and '<pass/>' not in text.lower() and '<say>' not in text.lower():
        text = f'<say>{text}</say>'
    session = ps.Session(launcher_type=ps.LauncherTypes(launcher_type), launcher_id=gid)
    ev = events.NormalMessageResponded(
        launcher_type=launcher_type, launcher_id=gid, sender_id=sender_id, session=session,
        prefix='', response_text=text, finish_reason='stop', funcs_called=[],
    )
    ctx = context.EventContext.from_event(ev)
    run(handler(inst, events.NormalMessageResponded)(ctx))
    return ctx


def full_flow(inst, gid, msg_id, *parts, sender=(900000003, '阿明')):
    """收到 → (群规则) → 交给 Claude 前。返回第二个挂载点的 ctx，没走到则返回 None。"""
    first = received(inst, gid, msg_id, *parts, sender=sender)
    if first.is_prevented_default() or not rule_stage_passes(inst, first):
        return None
    return normal_from(inst, first, gid, msg_id, sender=sender)


# ---------- 只旁听 / 白名单 ----------

def test_listen_mode_never_lets_anything_through(listener):
    ctx = received(listener, GROUP, 1, m.At(target=SELF), m.Plain(text='在吗'))
    assert ctx.is_prevented_default()
    record = listener._buffer.recent(GROUP)[0]
    assert record.text == '@大小姐 在吗' and record.reply_to == '大小姐'
    assert responded(listener, GROUP, '你好').is_prevented_default()


def test_non_whitelisted_group_is_blocked_and_not_recorded(listener):
    listener.config['reply-mode'] = 'mention'
    assert received(listener, OTHER_GROUP, 2, m.At(target=SELF)).is_prevented_default()
    assert listener._buffer is None or listener._buffer.count(OTHER_GROUP) == 0


def test_mention_mode_requires_secret(listener):
    listener.config.update({'reply-mode': 'mention', 'gate-secret': 'short'})
    assert full_flow(listener, GROUP, 3, m.At(target=SELF)) is None


# ---------- 暗号与完整流程 ----------

def test_at_flow_survives_rule_stage_removing_at(listener):
    listener.config['reply-mode'] = 'mention'
    assert received(listener, GROUP, 10, m.Plain(text='今晚打游戏吗')).is_prevented_default()
    ctx = full_flow(listener, GROUP, 11, m.At(target=SELF), m.Plain(text='你来不来'))
    assert ctx is not None and not ctx.is_prevented_default()
    prompt = ctx.event.user_message_alter
    assert '[情况：有人 @了你（明确在叫你）]' in prompt
    assert '阿明：今晚打游戏吗' in prompt
    assert '阿明 → 大小姐：@大小姐 你来不来' in prompt
    assert 'QGC' not in prompt  # 暗号不会出现在给 Claude 的内容里


def test_message_without_marker_never_reaches_claude(listener):
    listener.config['reply-mode'] = 'mention'
    # 模拟"排队追加"之类绕过第一个挂载点的路径：消息里没有暗号
    ctx = _group_event(events.GroupNormalMessageReceived, GROUP, 12, [m.Plain(text='大小姐在吗')], text_message='')
    run(handler(listener, events.GroupNormalMessageReceived)(ctx))
    assert ctx.is_prevented_default()
    # 伪造的暗号也不行
    fake = m.Plain(text='⁣QGC:000000000000:at:13:0000000000000000⁣在吗')
    ctx = _group_event(events.GroupNormalMessageReceived, GROUP, 13, [fake], text_message='')
    run(handler(listener, events.GroupNormalMessageReceived)(ctx))
    assert ctx.is_prevented_default()


def test_reply_to_bot_and_quoted_original(listener):
    listener.config['reply-mode'] = 'mention'
    quote = m.Quote(sender_id=SELF, origin=m.MessageChain([m.Plain(text='本小姐说的话' * 10)]))
    ctx = full_flow(listener, GROUP, 20, quote, m.Plain(text='真的假的'))
    prompt = ctx.event.user_message_alter
    assert '[情况：有人回复了你的消息（明确在跟你说话）]' in prompt
    assert '[它回复的原消息] 大小姐：' + '本小姐说的话' * 10 in prompt  # 原文完整，不截断


def test_weak_mention_addressed_to_others_is_ignored(listener):
    listener.config.update({'reply-mode': 'mention'})
    assert full_flow(listener, GROUP, 30, m.At(target='111'), m.Plain(text='你别学大小姐嘴硬')) is None
    ctx = full_flow(listener, GROUP, 31, m.Plain(text='大小姐今天怎么不说话'))
    assert ctx is not None and '不一定是在跟你说话' in ctx.event.user_message_alter


def test_weak_mentions_limited_per_minute(listener):
    listener.config.update({'reply-mode': 'mention', 'weak-per-minute': 2})
    assert full_flow(listener, GROUP, 40, m.Plain(text='大小姐在吗')) is not None
    assert full_flow(listener, GROUP, 41, m.Plain(text='大小姐说句话')) is not None
    assert full_flow(listener, GROUP, 42, m.Plain(text='大小姐睡了？')) is None
    assert full_flow(listener, GROUP, 43, m.At(target=SELF), m.Plain(text='强点名照样放行')) is not None


def test_strong_limit_per_user_then_one_notice(listener, sent):
    listener.config.update({'reply-mode': 'mention', 'user-daily-strong-limit': 2})
    assert full_flow(listener, GROUP, 50, m.At(target=SELF)) is not None
    assert full_flow(listener, GROUP, 51, m.At(target=SELF)) is not None
    assert full_flow(listener, GROUP, 52, m.At(target=SELF)) is None
    assert len(sent) == 1 and sent[0][1] is True  # 引用原消息回一句预设话术
    assert full_flow(listener, GROUP, 53, m.At(target=SELF)) is None
    assert len(sent) == 1  # 当天只提醒一次
    assert listener._buffer.last_self(GROUP).text == sent[0][0]
    # 别人不受影响
    assert full_flow(listener, GROUP, 54, m.At(target=SELF), sender=(777, '阿杰')) is not None


def test_custom_notices(listener, sent):
    listener.config.update({'reply-mode': 'mention', 'user-daily-strong-limit': 0,
                            'over-limit-replies': '今天第 {n} 次了，不理你'})
    assert full_flow(listener, GROUP, 60, m.At(target=SELF)) is None
    assert sent == [('今天第 0 次了，不理你', True)]


# ---------- 输出 ----------

def test_response_filtering_and_self_record(listener):
    listener.config['reply-mode'] = 'mention'
    full_flow(listener, GROUP, 70, m.At(target=SELF), m.Plain(text='在吗'))
    ctx = responded(listener, GROUP, '**来啊** 详情见 https://a.b/c')
    assert str(ctx.event.reply_message_chain) == '来啊 详情见'
    last = listener._buffer.last_self(GROUP)
    assert last.text == '来啊 详情见' and last.reply_to == '阿明'
    assert responded(listener, GROUP, '<pass/>').is_prevented_default()


def test_private_responses_are_untouched(listener):
    listener.config['reply-mode'] = 'mention'
    ctx = responded(listener, 'wxid_abc', '<pass/>', launcher_type='person')
    assert not ctx.is_prevented_default() and ctx.event.reply_message_chain is None


def test_commands_blocked_in_whitelisted_group(listener):
    ctx = _group_event(events.GroupCommandSent, GROUP, 80, [m.Plain(text='!help')],
                       command='help', params=[], text_message='!help', is_admin=False)
    run(handler(listener, events.GroupCommandSent)(ctx))
    assert ctx.is_prevented_default() and ctx.event.reply_message_chain is None
    other = _group_event(events.GroupCommandSent, OTHER_GROUP, 81, [m.Plain(text='!help')],
                         command='help', params=[], text_message='!help', is_admin=False)
    run(handler(listener, events.GroupCommandSent)(other))
    assert not other.is_prevented_default()


# ---------- 看图 / 表情 ----------

def test_vision_saves_images_and_lists_paths(listener, tmp_path):
    inbox = tmp_path / 'inbox'
    listener.config.update({'reply-mode': 'mention', 'vision-enabled': True, 'inbox-dir': str(inbox)})
    ctx = full_flow(listener, GROUP, 90, m.At(target=SELF), m.Plain(text='这图啥意思'),
                    m.Image(base64=base64.b64encode(PNG).decode()))
    prompt = ctx.event.user_message_alter
    assert '[本条附带的图片，需要时用 Read 查看]' in prompt
    saved = [line[2:] for line in prompt.splitlines() if line.startswith('- ')]
    assert len(saved) == 1 and saved[0].startswith(str(inbox)) and saved[0].endswith('.png')


def test_vision_off_lists_no_paths(listener):
    listener.config['reply-mode'] = 'mention'
    ctx = full_flow(listener, GROUP, 91, m.At(target=SELF), m.Image(url='https://gchat.qpic.cn/x'))
    assert '[本条附带的图片' not in ctx.event.user_message_alter


def test_stickers_and_faces_in_reply(listener, tmp_path):
    stickers = tmp_path / 'stickers'
    stickers.mkdir()
    (stickers / '狗头.png').write_bytes(PNG)
    listener.config.update({
        'reply-mode': 'mention', 'stickers-enabled': True, 'stickers-dir': str(stickers),
        'qq-faces-enabled': True, 'daily-sticker-limit-per-group': 1,
    })
    ctx = full_flow(listener, GROUP, 100, m.At(target=SELF), m.Plain(text='你不行吧'))
    assert '[可用表情包：狗头]' in ctx.event.user_message_alter
    ctx = responded(listener, GROUP, '再说一遍试试？[QQ表情:傲慢][表情:狗头]')
    assert [c.type for c in ctx.event.reply_message_chain] == ['Plain', 'Face', 'Image']
    assert listener._buffer.last_self(GROUP).text == '再说一遍试试？[QQ表情：傲慢][表情包：狗头]'
    ctx = responded(listener, GROUP, '哼 [表情:狗头]')  # 每日上限 1：第二次只剩文字
    assert [c.type for c in ctx.event.reply_message_chain] == ['Plain']


def test_sticker_only_reply_and_unknown_tag(listener, tmp_path):
    stickers = tmp_path / 'stickers'
    stickers.mkdir()
    (stickers / '无语.gif').write_bytes(b'GIF89a')
    listener.config.update({'reply-mode': 'mention', 'stickers-enabled': True, 'stickers-dir': str(stickers)})
    assert [c.type for c in responded(listener, GROUP, '[表情:无语]').event.reply_message_chain] == ['Image']
    assert responded(listener, GROUP, '[表情:不存在]').is_prevented_default()


def test_tokens_stripped_when_features_off(listener):
    listener.config['reply-mode'] = 'mention'
    ctx = responded(listener, GROUP, '好吧[QQ表情:捂脸][表情:狗头]')
    assert str(ctx.event.reply_message_chain) == '好吧'


class FakePlugin:
    def __init__(self, names):
        self.names = names
        self.calls = 0

    async def call_platform_api(self, bot_uuid, action, params):
        self.calls += 1
        assert action == 'get_group_member_info' and str(params['user_id']) == SELF
        name = self.names.get(str(params['group_id']))
        if isinstance(name, Exception):
            raise name
        return {'display_name': name, 'user': {'nickname': '青年'}}


def test_group_nickname_follows_and_is_cached(listener, monkeypatch):
    async def bot_uuid(self):
        return 'bot-uuid'

    monkeypatch.setattr(context.EventContext, 'get_bot_uuid', bot_uuid)
    fake = FakePlugin({str(GROUP): '69'})
    listener.plugin = fake
    listener.config.update({'reply-mode': 'mention', 'persona-aliases': 'Claude'})
    ctx = full_flow(listener, GROUP, 200, m.Plain(text='69 你在吗'))
    assert ctx is not None and '提到了你的名字' in ctx.event.user_message_alter
    assert '[你在本群的昵称：69]' in ctx.event.user_message_alter
    assert full_flow(listener, GROUP, 201, m.Plain(text='1969 年')) is None
    assert fake.calls == 1  # 10 分钟内只查一次
    # 群昵称改了：过期后重新查询
    fake.names[str(GROUP)] = '大小姐号'
    listener._nicknames[str(GROUP)] = ('69', 0)
    assert full_flow(listener, GROUP, 202, m.Plain(text='大小姐号 出来')) is not None
    assert full_flow(listener, GROUP, 203, m.Plain(text='69 出来')) is None  # 旧昵称不再算
    # @机器人时显示当前群昵称
    received(listener, GROUP, 204, m.At(target=SELF), m.Plain(text='嗨'))
    assert listener._buffer.get(GROUP, '204').text == '@大小姐号 嗨'


def test_nickname_lookup_failure_keeps_old_value(listener, monkeypatch):
    async def bot_uuid(self):
        return 'bot-uuid'

    monkeypatch.setattr(context.EventContext, 'get_bot_uuid', bot_uuid)
    listener.plugin = FakePlugin({str(GROUP): RuntimeError('boom')})
    listener.config['reply-mode'] = 'mention'
    listener._nicknames[str(GROUP)] = ('69', 0)
    assert full_flow(listener, GROUP, 210, m.Plain(text='69 在吗')) is not None


def test_owner_mention_flow(listener):
    listener.config.update({'reply-mode': 'mention', 'owner-qq': '900000002', 'owner-aliases': '店长'})
    ctx = full_flow(listener, GROUP, 220, m.Plain(text='店长今天又鸽了'))
    assert ctx is not None and '有人提到了你的主人' in ctx.event.user_message_alter
    assert full_flow(listener, GROUP, 221, m.Plain(text='店长本人在此'), sender=(900000002, '店长本人')) is None


def test_reasoning_outside_say_tag_is_dropped(listener):
    listener.config['reply-mode'] = 'mention'
    raw = '主人在叫Claude，明确问。回一句。' + chr(10) * 2 + '<say>信？那是鲸鱼吧。</say>'
    ctx = responded(listener, GROUP, raw, raw=True)
    assert str(ctx.event.reply_message_chain) == '信？那是鲸鱼吧'


def test_reply_without_say_tag_is_not_sent(listener):
    listener.config['reply-mode'] = 'mention'
    assert responded(listener, GROUP, '主人在叫我。信？那是鲸鱼吧。', raw=True).is_prevented_default()


def test_reply_with_email_is_not_sent(listener):
    listener.config['reply-mode'] = 'mention'
    assert responded(listener, GROUP, '主人邮箱是 someone@example.com 哦').is_prevented_default()
    assert listener._buffer is None or listener._buffer.last_self(GROUP) is None


# ---------- Claude 出错（包装脚本输出 <qgc-error/>） ----------

def _error(kind):
    return f'<qgc-error kind="{kind}"/>'


def test_error_on_strong_mention_gets_preset_reply(listener, sent):
    listener.config['reply-mode'] = 'mention'
    assert full_flow(listener, GROUP, 300, m.At(target=SELF), m.Plain(text='在吗')) is not None
    ctx = responded(listener, GROUP, _error('limit'), raw=True)
    mod = importlib.import_module('components.event_listener.companion')
    text = str(ctx.event.reply_message_chain)
    assert sent[-1] == (text, True) and text in mod.DEFAULT_ERROR_REPLIES['limit']
    assert listener._buffer.last_self(GROUP).text == text


def test_error_on_weak_mention_is_silent_by_default(listener, sent):
    listener.config.update({'reply-mode': 'mention', 'persona-aliases': 'Claude'})
    assert full_flow(listener, GROUP, 310, m.Plain(text='Claude 在吗')) is not None
    assert responded(listener, GROUP, _error('other'), raw=True).is_prevented_default()
    listener.config['error-reply-weak'] = True
    assert full_flow(listener, GROUP, 311, m.Plain(text='Claude 在吗')) is not None
    responded(listener, GROUP, _error('other'), raw=True)
    assert len(sent) == 1 and sent[0][1] is False  # 弱点名不引用


def test_error_reply_cooldown_and_custom_lines(listener, sent):
    listener.config.update({'reply-mode': 'mention', 'error-replies-refused': '不答|换一个'})
    assert full_flow(listener, GROUP, 320, m.At(target=SELF), m.Plain(text='a')) is not None
    first = responded(listener, GROUP, _error('refused'), raw=True)
    assert str(first.event.reply_message_chain) in ('不答', '换一个')
    assert full_flow(listener, GROUP, 321, m.At(target=SELF), m.Plain(text='b')) is not None
    assert responded(listener, GROUP, _error('refused'), raw=True).is_prevented_default()  # 冷却中
    listener.config['error-cooldown-minutes'] = 0
    assert full_flow(listener, GROUP, 322, m.At(target=SELF), m.Plain(text='c')) is not None
    responded(listener, GROUP, _error('refused'), raw=True)
    assert len(sent) == 2


def test_error_without_known_trigger_is_silent(listener):
    listener.config['reply-mode'] = 'mention'
    assert responded(listener, GROUP, _error('limit'), raw=True).is_prevented_default()


# ---------- 引用与句号 ----------

def test_only_strong_mentions_quote_the_original(listener, sent):
    listener.config.update({'reply-mode': 'mention', 'persona-aliases': 'Claude'})
    assert full_flow(listener, GROUP, 600, m.At(target=SELF), m.Plain(text='在吗')) is not None
    responded(listener, GROUP, '在')
    assert full_flow(listener, GROUP, 601, m.Plain(text='Claude 出列')) is not None
    responded(listener, GROUP, '大小姐驾到')
    assert sent == [('在', True), ('大小姐驾到', False)]


def test_final_period_is_dropped(listener, sent):
    listener.config['reply-mode'] = 'mention'
    assert full_flow(listener, GROUP, 610, m.At(target=SELF), m.Plain(text='a')) is not None
    responded(listener, GROUP, '到。刚补完妆。')
    assert full_flow(listener, GROUP, 611, m.At(target=SELF), m.Plain(text='b')) is not None
    responded(listener, GROUP, '……')
    assert [text for text, _ in sent] == ['到。刚补完妆', '……']
