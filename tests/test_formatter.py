import datetime

import langbot_plugin.api.entities.builtin.platform.message as m

from companion.formatter import format_chain


def chain(*parts):
    return m.MessageChain([m.Source(id=1, time=datetime.datetime.now()), *parts])


def test_plain_and_at_with_names():
    names = {'900000003': '小林'}
    out = format_chain(chain(m.At(target='900000003'), m.Plain(text='你现在开了没有？')), name_of=names.get)
    assert out == '@小林 你现在开了没有？'


def test_unknown_at_falls_back_to_qq_and_at_all():
    out = format_chain(chain(m.At(target='900000001'), m.AtAll(), m.Plain(text='上号')))
    assert out == '@900000001 @全体成员 上号'


def test_links_are_replaced():
    out = format_chain(chain(m.Plain(text='看这个 https://example.com/a?b=1 好玩')))
    assert out == '看这个 [链接] 好玩'


def test_media_placeholders():
    out = format_chain(chain(
        m.Image(url='https://multimedia.nt.qq.com.cn/x'),
        m.Voice(url='x'),
        m.File(name='报告.pdf'),
        m.Forward(),
    ))
    assert out == '[图片][语音][文件：报告.pdf][聊天记录]'


def test_faces_and_stickers():
    assert format_chain(chain(m.Face(face_id=264, face_name='捂脸'))) == '[QQ表情：捂脸]'
    assert format_chain(chain(m.Face(face_id=12345678, face_name='[狗头]'))) == '[表情包：狗头]'
    assert format_chain(chain(m.Face(face_id=12345678, face_name='[动画表情]'))) == '[表情包]'
    assert format_chain(chain(m.Face(face_type='dice', face_id=3, face_name='骰子'))) == '[骰子]'


def test_unknown_segments():
    assert format_chain(chain(m.Unknown(text="video:{'file': 'a.mp4'}"))) == '[视频]'
    assert format_chain(chain(m.Unknown(text="mface:{'summary': '[打call]', 'emoji_id': 'x'}"))) == '[表情：打call]'
    assert format_chain(chain(m.Unknown(text='markdown:{}'))) == '[不支持的消息]'


def test_quote_preview_truncated_and_named():
    origin = m.MessageChain([m.Plain(text='发育差强行打到5层，最后因为数值不够遗憾离场了，真的太可惜了下次再来')])
    out = format_chain(
        chain(m.Quote(sender_id='1223231416', origin=origin), m.Plain(text='当刷襁褓了')),
        name_of={'1223231416': '白锦鲤'}.get,
    )
    assert out.startswith('（回复 白锦鲤：发育差强行打到5层')
    assert '…）' in out
    assert out.endswith('当刷襁褓了')


def test_dicts_and_empty():
    assert format_chain([{'type': 'Plain', 'text': '  你好\n 呀 '}]) == '你好 呀'
    assert format_chain(chain()) == '[空消息]'
