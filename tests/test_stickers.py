from companion.faces import face_id
from companion.stickers import StickerLibrary, parse_reply


def test_library_tags_and_refresh(tmp_path):
    (tmp_path / '狗头.png').write_bytes(b'x')
    (tmp_path / '无语.gif').write_bytes(b'x')
    (tmp_path / 'notes.txt').write_text('ignore')
    lib = StickerLibrary(tmp_path)
    assert sorted(lib.tags()) == ['无语', '狗头']
    assert lib.path_of('狗头').name == '狗头.png'
    assert lib.path_of('不存在') is None
    (tmp_path / '打call.webp').write_bytes(b'x')
    assert '打call' in lib.tags()


def test_missing_dir(tmp_path):
    assert StickerLibrary(tmp_path / 'nope').tags() == []


def test_parse_reply_tokens():
    out = parse_reply('哼，才不是 [QQ表情:傲慢] 特意帮你 [表情：狗头][QQ表情:不存在][QQ表情:捂脸][QQ表情:doge]')
    assert out.text == '哼，才不是  特意帮你 '
    assert out.stickers == ['狗头']
    assert out.faces == [face_id('傲慢'), face_id('捂脸')]  # 最多两个，认不得的忽略


def test_face_lookup():
    assert face_id('捂脸') == 264
    assert face_id('/doge') == 179 and face_id('DOGE') == 179
    assert face_id('哼') == 102
    assert face_id('没有这个') is None
