import re
import time

import langbot_plugin.api.entities.builtin.platform.message as m

from companion.buffer import MessageBuffer
from companion.gate import (
    GROUP_DAILY,
    OVER_USER_LIMIT,
    PASSED,
    TOTAL_DAILY,
    WEAK_RATE,
    Limits,
    check_limits,
    detect_trigger,
    gate_regexp,
    make_marker,
    notice_already_sent,
    parse_aliases,
    read_marker,
    start_of_today,
    strip_marker,
)

SELF = '900000001'
SECRET = 'a' * 24


def test_strong_triggers():
    assert detect_trigger([m.At(target=SELF), m.Plain(text='在吗')], SELF, []) == 'at'
    quote = m.Quote(sender_id=SELF, origin=m.MessageChain([m.Plain(text='我说的')]))
    assert detect_trigger([quote, m.Plain(text='真的？')], SELF, []) == 'reply'
    # @了别人也 @了我：仍然是强点名
    assert detect_trigger([m.At(target='123'), m.At(target=SELF)], SELF, []) == 'at'


def test_weak_trigger_and_digit_boundaries():
    assert detect_trigger([m.Plain(text='大小姐你怎么看')], SELF, ['大小姐']) == 'name'
    assert detect_trigger([m.Plain(text='1969年的事')], SELF, ['69']) is None
    assert detect_trigger([m.Plain(text='69 你在吗')], SELF, ['69']) == 'name'


def test_name_mention_addressed_to_others_is_not_a_trigger():
    # A @B 说"你别学大小姐嘴硬"
    assert detect_trigger([m.At(target='123'), m.Plain(text='你别学大小姐嘴硬')], SELF, ['大小姐']) is None
    # A 回复 B 的消息说"大小姐说得对"
    quote = m.Quote(sender_id='123', origin=m.MessageChain([m.Plain(text='x')]))
    assert detect_trigger([quote, m.Plain(text='大小姐说得对')], SELF, ['大小姐']) is None
    assert detect_trigger([m.AtAll(), m.Plain(text='大小姐也来')], SELF, ['大小姐']) is None


def test_parse_aliases_drops_single_chars():
    assert parse_aliases('大小姐, 6，六六 ;Claude') == ['大小姐', '六六', 'Claude']
    assert parse_aliases('') == []


def test_limits_from_config_fallbacks():
    lim = Limits.from_config({'user-daily-strong-limit': '5', 'weak-per-minute': 'x', 'daily-limit-total': -3})
    assert lim == Limits(per_user_strong_daily=5, weak_per_minute=3, per_group_daily=100, total_daily=300)


def test_strong_per_user_daily_limit_has_no_rate_limit(tmp_path):
    buf = MessageBuffer(tmp_path)
    now = time.time()
    lim = Limits(per_user_strong_daily=3)
    for _ in range(3):  # 同一秒内连续 3 次强点名都放行
        assert check_limits(buf, 1, 'u1', 'at', lim, now) is None
        buf.record_trigger(1, 'u1', 'strong', PASSED, now)
    assert check_limits(buf, 1, 'u1', 'reply', lim, now) == OVER_USER_LIMIT
    assert check_limits(buf, 1, 'u2', 'at', lim, now) is None  # 别人不受影响
    buf.close()


def test_weak_per_minute(tmp_path):
    buf = MessageBuffer(tmp_path)
    now = time.time()
    lim = Limits(weak_per_minute=2)
    buf.record_trigger(1, 'u1', 'weak', PASSED, now - 10)
    buf.record_trigger(1, 'u2', 'weak', PASSED, now - 5)
    assert check_limits(buf, 1, 'u3', 'name', lim, now) == WEAK_RATE
    assert check_limits(buf, 1, 'u3', 'name', lim, now + 61) is None
    assert check_limits(buf, 2, 'u3', 'name', lim, now) is None  # 别的群不受影响
    assert check_limits(buf, 1, 'u3', 'at', lim, now) is None  # 强点名不受每分钟限制
    buf.close()


def test_daily_safety_caps(tmp_path):
    buf = MessageBuffer(tmp_path)
    now = time.time()
    lim = Limits(per_group_daily=1, total_daily=2)
    buf.add(group_id=1, message_id='s1', sender_id=SELF, sender_name='', text='a', ts=now, is_self=True)
    assert check_limits(buf, 1, 'u1', 'at', lim, now) == GROUP_DAILY
    buf.add(group_id=2, message_id='s2', sender_id=SELF, sender_name='', text='b', ts=now, is_self=True)
    assert check_limits(buf, 3, 'u1', 'at', lim, now) == TOTAL_DAILY
    assert start_of_today(now) <= now
    buf.close()


def test_notice_once_per_user_per_day(tmp_path):
    buf = MessageBuffer(tmp_path)
    assert not notice_already_sent(buf, 1, 'u1')
    buf.record_trigger(1, 'u1', 'strong', 'notice')
    assert notice_already_sent(buf, 1, 'u1')
    assert not notice_already_sent(buf, 1, 'u2')
    buf.close()


def test_marker_roundtrip_and_tamper():
    marker = make_marker(SECRET, 'at', '42')
    assert re.match(gate_regexp(SECRET), marker + '你好')
    assert read_marker(marker + '你好', SECRET) == ('at', '42')
    assert read_marker(marker.replace(':42:', ':43:'), SECRET) is None    # 改消息 ID 无效
    assert read_marker(marker, 'b' * 24) is None                          # 换了密钥就失效
    assert read_marker(marker.replace(':at:', ':reply:'), SECRET) is None  # 改触发类型无效
    assert read_marker('你好', SECRET) is None
    assert read_marker(marker, '') is None
    assert strip_marker(marker + '你好') == '你好'
    # 不知道密钥的人猜不出流水线正则要求的标签
    assert not re.match(gate_regexp(SECRET), make_marker('c' * 24, 'at', '42'))
    # 回退用的消息 ID（"发送者-时间"）里的特殊字符会被规整
    odd = make_marker(SECRET, 'name', '900000002-1792739965.0')
    assert read_marker(odd, SECRET) == ('name', '900000002-1792739965.0')


OWNER = '900000002'


def test_owner_alias_is_weak_trigger_except_from_owner():
    chain = [m.Plain(text='店长又在摸鱼了')]
    assert detect_trigger(chain, SELF, [], owner_aliases=['店长'], sender_id='111', owner_qq=OWNER) == 'owner'
    assert detect_trigger(chain, SELF, [], owner_aliases=['店长'], sender_id=OWNER, owner_qq=OWNER) is None
    at_other = [m.At(target='222'), m.Plain(text='店长说你')]
    assert detect_trigger(at_other, SELF, [], owner_aliases=['店长'], sender_id='111', owner_qq=OWNER) is None
    both = [m.Plain(text='claude 你主人店长呢')]
    assert detect_trigger(both, SELF, ['Claude'], owner_aliases=['店长'], sender_id='111', owner_qq=OWNER) == 'name'


def test_claude_alias_case_insensitive():
    for text in ('Claude 你怎么看', 'CLAUDE呢', '问问claude'):
        assert detect_trigger([m.Plain(text=text)], SELF, ['Claude']) == 'name', text
    assert detect_trigger([m.Plain(text='ClaudeCode 好用吗')], SELF, ['Claude']) is None
