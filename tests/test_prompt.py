import time

from companion.buffer import MessageBuffer
from companion.prompt import MAX_LINE_CHARS, build_prompt, select_context


def fill(buf, now):
    buf.add(group_id=1, message_id='old', sender_id=7, sender_name='小王', text='三小时前', ts=now - 3 * 3600)
    buf.add(group_id=1, message_id='a', sender_id=7, sender_name='小王', text='今晚打游戏吗', ts=now - 300)
    buf.add(group_id=1, message_id='s', sender_id=9, sender_name='大小姐', text='哼，看心情', ts=now - 200,
            is_self=True, reply_to='小王')
    buf.add(group_id=1, message_id='b', sender_id=8, sender_name='阿杰', text='我加班', ts=now - 100, reply_to='小王')
    buf.add(group_id=1, message_id='c', sender_id=7, sender_name='小王', text='@大小姐 那你呢', ts=now - 10,
            reply_to='大小姐')


def test_context_is_time_window_and_count_bounded(tmp_path):
    buf = MessageBuffer(tmp_path)
    now = time.time()
    fill(buf, now)
    assert [r.message_id for r in select_context(buf, 1, now)] == ['a', 's', 'b', 'c']
    assert [r.message_id for r in select_context(buf, 1, now, max_messages=2)] == ['b', 'c']
    assert [r.message_id for r in select_context(buf, 1, now, window_seconds=4 * 3600)][0] == 'old'
    buf.close()


def test_build_prompt_layout_with_addressees(tmp_path):
    buf = MessageBuffer(tmp_path)
    now = time.time()
    fill(buf, now)
    current = buf.get(1, 'c')
    out = build_prompt(group_name='测试群', trigger='at', context=select_context(buf, 1, now), current=current,
                       quoted='小王：今晚打游戏吗')
    lines = out.splitlines()
    assert lines[0] == '[群：测试群]'
    assert lines[1] == '[情况：有人 @了你（明确在叫你）]'
    assert any(line.endswith('你 → 小王：哼，看心情') for line in lines)
    assert any(line.endswith('阿杰 → 小王：我加班') for line in lines)
    assert any(line.endswith('小王：今晚打游戏吗') and '→' not in line for line in lines)
    i = lines.index('[刚刚这条]')
    assert lines[i + 1].endswith('小王 → 大小姐：@大小姐 那你呢')
    assert lines[i + 2] == '[它回复的原消息] 小王：今晚打游戏吗'
    assert out.count('那你呢') == 1
    assert '<pass/>' in lines[-1]
    buf.close()


def test_weak_label_and_long_lines(tmp_path):
    buf = MessageBuffer(tmp_path)
    now = time.time()
    buf.add(group_id=1, message_id='x', sender_id=7, sender_name='小王', text='长' * 500, ts=now - 5)
    buf.add(group_id=1, message_id='y', sender_id=7, sender_name='小王', text='大小姐' + '啊' * 300, ts=now)
    out = build_prompt(group_name='', trigger='name', context=select_context(buf, 1, now), current=buf.get(1, 'y'))
    assert '不一定是在跟你说话' in out
    assert ('长' * MAX_LINE_CHARS + '…') in out and ('长' * (MAX_LINE_CHARS + 1)) not in out
    assert '啊' * 300 in out  # 当前这条不截断
    assert out.startswith('[群：1]')
    buf.close()


def test_owner_lines_are_marked(tmp_path):
    buf = MessageBuffer(tmp_path)
    now = time.time()
    buf.add(group_id=1, message_id='o', sender_id='900000002', sender_name='店长本人', text='我去开会了', ts=now - 60)
    buf.add(group_id=1, message_id='x', sender_id=7, sender_name='小王', text='店长又跑了', ts=now)
    out = build_prompt(group_name='g', trigger='owner', context=select_context(buf, 1, now), current=buf.get(1, 'x'),
                       owner_qq='900000002')
    assert '店长本人（主人）：我去开会了' in out
    assert '小王：店长又跑了' in out and '小王（主人）' not in out
    assert '有人提到了你的主人' in out
    buf.close()
