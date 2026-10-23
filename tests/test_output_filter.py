from companion.output_filter import clean_reply, contains_email, extract_say


def test_pass_and_empty():
    assert clean_reply('<pass/>') is None
    assert clean_reply('  <PASS/>  ') is None
    assert clean_reply('嗯 <pass/> 算了') is None
    assert clean_reply('') is None
    assert clean_reply(None) is None


def test_strip_links_markdown_and_code():
    out = clean_reply('**好的**，看 https://x.com/a 这个\n```python\nprint(1)\n```\n# 标题\n`代码`')
    assert out == '好的，看  这个\n\n标题\n代码'


def test_length_cap_and_only_link():
    assert clean_reply('啊' * 50, max_chars=20) == '啊' * 20 + '…'
    assert clean_reply('https://example.com') is None


def test_extract_say():
    assert extract_say('想法' + chr(10) + '<say>你好</say>') == '你好'
    assert extract_say('<SAY> 第一句 </SAY>旁白<say>第二句</say>') == '第一句' + chr(10) + '第二句'
    assert extract_say('没有标签') is None
    assert extract_say('<say>  </say>') is None
    assert extract_say(None) is None


def test_contains_email():
    assert contains_email('联系 a.b-c@example.com')
    assert not contains_email('@大小姐 你好')


def test_extract_error():
    from companion.output_filter import extract_error
    assert extract_error('<qgc-error kind="limit"/>') == 'limit'
    assert extract_error('<qgc-error kind="weird"/>') == 'other'
    assert extract_error('<say>hi</say>') is None
    assert extract_error(None) is None


def test_drop_final_period():
    from companion.output_filter import drop_final_period
    assert drop_final_period('好。') == '好'
    assert drop_final_period('ok.') == 'ok'
    assert drop_final_period('……') == '……'
    assert drop_final_period('等等...') == '等等...'
    assert drop_final_period('真的？') == '真的？'
