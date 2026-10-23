import asyncio
import base64
import os
import time

import httpx
import langbot_plugin.api.entities.builtin.platform.message as m

from companion.media import collect_images, host_allowed, purge_inbox, save_images, sniff_image

PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 32
JPG = b'\xff\xd8\xff\xe0' + b'\x00' * 32


def test_sniff_image():
    assert sniff_image(PNG) == 'png'
    assert sniff_image(JPG) == 'jpg'
    assert sniff_image(b'GIF89a....') == 'gif'
    assert sniff_image(b'RIFF\x00\x00\x00\x00WEBPVP8 ') == 'webp'
    assert sniff_image(b'<html>not an image') is None


def test_host_allowlist():
    assert host_allowed('https://multimedia.nt.qq.com.cn/download?appid=1407&fileid=x')
    assert host_allowed('https://gchat.qpic.cn/gchatpic_new/0/0-0-ABC/0')
    assert host_allowed('http://c2cpicdw.qpic.cn/offpic_new/1')
    assert not host_allowed('https://evil.com/x.png')
    assert not host_allowed('https://qq.com.evil.com/x.png')
    assert not host_allowed('file:///C:/secret.png')
    assert not host_allowed('http://127.0.0.1:5300/api')


def test_collect_includes_quoted_images_and_limits():
    quote = m.Quote(sender_id='1', origin=m.MessageChain([m.Image(url='https://gchat.qpic.cn/a'), m.Plain(text='x')]))
    chain = [m.Image(url='https://gchat.qpic.cn/b'), quote, m.Image(url='https://gchat.qpic.cn/c')]
    assert [i.url for i in collect_images(chain)] == [
        'https://gchat.qpic.cn/b',
        'https://gchat.qpic.cn/a',
        'https://gchat.qpic.cn/c',
    ]
    assert len(collect_images(chain, limit=2)) == 2


def _client(routes):
    def handler(request):
        body = routes.get(str(request.url))
        return httpx.Response(200, content=body) if body is not None else httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_save_images_download_base64_and_rejections(tmp_path):
    good = 'https://gchat.qpic.cn/good'
    html = 'https://gchat.qpic.cn/html'
    big = 'https://gchat.qpic.cn/big'
    client = _client({good: JPG, html: b'<html>', big: PNG + b'\x00' * 2048})
    images = [
        m.Image(url=good),
        m.Image(url=html),  # 不是图片
        m.Image(url=big),  # 超过大小
        m.Image(url='https://evil.com/x.png'),  # 域名不在白名单
        m.Image(base64=base64.b64encode(PNG).decode()),
    ]

    async def go():
        async with client:
            return await save_images(images, tmp_path, max_bytes=1024, client=client)

    saved = asyncio.run(go())
    assert [p.suffix for p in saved] == ['.jpg', '.png']
    assert all(p.parent.parent == tmp_path for p in saved)
    assert saved[0].read_bytes() == JPG


def test_purge_inbox(tmp_path):
    old = tmp_path / '2026-10-01' / 'a.jpg'
    new = tmp_path / '2026-10-23' / 'b.png'
    for path in (old, new):
        path.parent.mkdir(parents=True)
        path.write_bytes(JPG)
    stale = time.time() - 10 * 86400
    os.utime(old, (stale, stale))
    assert purge_inbox(tmp_path, 3) == 1
    assert not old.exists() and not old.parent.exists() and new.exists()
