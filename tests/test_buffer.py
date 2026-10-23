import time

from companion.buffer import MessageBuffer


def test_add_dedup_recent_and_names(tmp_path):
    buf = MessageBuffer(tmp_path)
    assert buf.add(group_id=1, message_id=10, sender_id=7, sender_name='小王', text='第一句', ts=100)
    assert not buf.add(group_id=1, message_id=10, sender_id=7, sender_name='小王', text='第一句', ts=100)
    buf.add(group_id=1, message_id=11, sender_id=8, sender_name='阿杰', text='第二句', ts=101)
    buf.add(group_id=2, message_id=11, sender_id=8, sender_name='阿杰', text='别的群', ts=102)
    recent = buf.recent(1)
    assert [r.text for r in recent] == ['第一句', '第二句']
    assert buf.recent(1, limit=1)[0].text == '第二句'
    assert buf.name_of(1, 8) == '阿杰'
    assert buf.name_of(1, 999) is None
    assert buf.count(1) == 2 and buf.count() == 3
    buf.close()


def test_purge_old_records(tmp_path):
    buf = MessageBuffer(tmp_path, retention_days=1)
    buf.add(group_id=1, message_id=1, sender_id=7, sender_name='', text='很久以前', ts=time.time() - 3 * 86400)
    buf.add(group_id=1, message_id=2, sender_id=7, sender_name='', text='刚刚', ts=time.time())
    assert buf.purge() == 1
    assert [r.text for r in buf.recent(1)] == ['刚刚']
    buf.close()
