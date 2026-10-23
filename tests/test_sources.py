import asyncio
import base64
import json
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from zonevpn import dashboard, source_store, sources, state

LINK = 'vless://12345678-1234-1234-1234-123456789abc@node.example:443?security=tls&type=tcp#Test'


class LocalResolver(aiohttp.abc.AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        return [dict(hostname=host, host='127.0.0.1', port=port,
                     family=socket.AF_INET, proto=0, flags=0)]

    async def close(self):
        pass


class StoreTests(unittest.TestCase):
    def test_normalization_and_rejections(self):
        self.assertEqual(source_store.normalize_url(' HTTPS://EXAMPLE.COM:443/sub?a=1#fragment '), 'https://example.com/sub?a=1')
        for u in [None, [], 'ftp://example.com/x', 'https://u:p@example.com/',
                  'http://127.0.0.1/', 'http://169.254.169.254/', 'http://[::1]/',
                  'http://[::ffff:127.0.0.1]/', 'http://localhost/', 'http://host.local/',
                  'https://example.com:99999/', 'https://example.com/\nx', 'https://example.com/' + 'x' * 2048]:
            with self.subTest(url=u), self.assertRaises(ValueError):
                source_store.normalize_url(u)

    def test_atomic_write_failure_retains_previous(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(state, 'STATE_DIR', Path(tmp)):
            source_store.write_custom(['https://example.com/a'])
            with patch.object(source_store.os, 'replace', side_effect=OSError('disk failure')):
                with self.assertRaises(OSError):
                    source_store.write_custom(['https://example.com/b'])
            self.assertEqual(source_store.read_custom(), ['https://example.com/a'])
            self.assertEqual(len(list(Path(tmp).iterdir())), 1)

    def test_corrupt_store_is_not_silently_reset(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(state, 'STATE_DIR', Path(tmp)):
            for raw in [b'{broken', b'{}', b'[12]', b'x' * (source_store.MAX_STORE_BYTES + 1)]:
                (Path(tmp) / 'sources.json').write_bytes(raw)
                with self.assertRaises(ValueError):
                    source_store.read_custom()


class SourceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        sources._CACHE.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.state_patch = patch.object(state, 'STATE_DIR', Path(self.tmp.name))
        self.state_patch.start()
        self.etag_requests = []
        self.fail = False
        app = web.Application()

        async def good(request):
            return web.Response(text=LINK + '\n' + LINK)

        async def b64(request):
            return web.Response(text=base64.b64encode(LINK.encode()).decode())

        async def etag(request):
            self.etag_requests.append(request.headers.get('If-None-Match'))
            if self.fail:
                return web.Response(status=503)
            if request.headers.get('If-None-Match') == '"v1"':
                return web.Response(status=304)
            return web.Response(text=LINK, headers={'ETag': '"v1"'})

        async def large(request):
            resp = web.StreamResponse()
            await resp.prepare(request)
            await resp.write(b'x' * 1024)
            await resp.write_eof()
            return resp

        async def slow(request):
            await asyncio.sleep(.25)
            return web.Response(text=LINK)

        async def redirect(request):
            return web.Response(status=302, headers={'Location': 'http://127.0.0.1/private'})

        async def loop(request):
            return web.Response(status=302, headers={'Location': '/loop'})

        async def bad(request):
            return web.Response(status=503)

        async def html(request):
            return web.Response(text='<html>Sign in</html>')

        async def compressed(request):
            return web.Response(body=b'not inflated', headers={'Content-Encoding': 'gzip'})

        for path, handler in [('good', good), ('b64', b64), ('etag', etag), ('large', large),
                              ('slow', slow), ('redirect', redirect), ('loop', loop),
                              ('bad', bad), ('html', html), ('compressed', compressed)]:
            app.router.add_get('/' + path, handler)
        self.server = TestServer(app)
        await self.server.start_server()
        self.base = f'http://source.example:{self.server.port}/'
        self.session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=LocalResolver()),
                                             headers={'Accept-Encoding': 'identity'})

    async def asyncTearDown(self):
        await self.session.close()
        await self.server.close()
        self.state_patch.stop()
        self.tmp.cleanup()
        sources._CACHE.clear()

    async def fetch(self, path, health=None):
        return await sources._fetch_one(self.session, self.base + path, health)

    async def test_plain_base64_and_dedup(self):
        self.assertEqual(await self.fetch('b64'), [LINK])
        with patch.object(sources, 'PublicResolver', LocalResolver):
            result = await sources.collect([self.base + 'good', self.base + 'b64'])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].extra['src'], 'source.example/good')

    async def test_conditional_cache_and_failure_fallback(self):
        self.assertEqual(await self.fetch('etag'), [LINK])
        self.assertEqual(await self.fetch('etag'), [LINK])
        self.assertEqual(self.etag_requests, [None, '"v1"'])
        self.fail = True
        health = {}
        self.assertEqual(await self.fetch('etag', health), [LINK])
        self.assertEqual(next(iter(health.values()))['status'], 'cached')
        with patch.object(sources, 'CACHE_TTL', -1):
            self.assertEqual(await self.fetch('etag'), [])

    async def test_chunked_size_limit(self):
        with patch.object(sources, 'MAX_BODY', 128):
            self.assertEqual(await self.fetch('large'), [])

    async def test_content_length_limit(self):
        with patch.object(sources, 'MAX_BODY', 16):
            self.assertEqual(await self.fetch('good'), [])

    async def test_compression_html_bad_http_redirect_and_loop_isolated(self):
        for path in ['compressed', 'html', 'bad', 'redirect', 'loop']:
            with self.subTest(path=path):
                self.assertEqual(await self.fetch(path), [])
        self.assertTrue(await self.fetch('good'))

    async def test_timeout(self):
        with patch.object(sources, 'FETCH_TIMEOUT', .03):
            self.assertEqual(await self.fetch('slow'), [])
            self.assertTrue(await self.fetch('good'))

    async def test_dns_resolver_rejects_any_private_address(self):
        resolver = sources.PublicResolver()
        try:
            with patch.object(sources.ThreadedResolver, 'resolve', AsyncMock(return_value=[{'host': '8.8.8.8'}, {'host': '10.0.0.1'}])):
                with self.assertRaises(OSError):
                    await resolver.resolve('source.example')
            with patch.object(sources.ThreadedResolver, 'resolve', AsyncMock(return_value=[{'host': '8.8.8.8'}])):
                self.assertEqual((await resolver.resolve('source.example'))[0]['host'], '8.8.8.8')
        finally:
            await resolver.close()

    async def test_custom_added_and_removed_between_cycles(self):
        with patch.object(sources, 'PublicResolver', LocalResolver):
            source_store.write_custom([self.base + 'good'])
            self.assertEqual(len(await sources.collect([])), 1)
            source_store.write_custom([])
            self.assertEqual(await sources.collect([]), [])
            self.assertFalse(sources._CACHE)

    async def test_corrupt_custom_file_keeps_configured_sources_working(self):
        (Path(self.tmp.name) / 'sources.json').write_text('{bad')
        with patch.object(sources, 'PublicResolver', LocalResolver):
            self.assertEqual(len(await sources.collect([self.base + 'good'])), 1)

    async def test_removed_configured_source_not_fetched_or_cached_next_cycle(self):
        url = self.base + 'good'
        with patch.object(sources, 'PublicResolver', LocalResolver):
            self.assertEqual(len(await sources.collect([url])), 1)
            source_store.write_store([], [url])
            self.assertEqual(await sources.collect([url]), [])
            self.assertFalse(sources._CACHE)

    async def test_global_deadline_retains_completed_source(self):
        with patch.object(sources, 'PublicResolver', LocalResolver), patch.object(sources, 'CYCLE_TIMEOUT', .05):
            result = await sources.collect([self.base + 'good', self.base + 'slow'])
        self.assertEqual(len(result), 1)

    async def test_cache_byte_budget(self):
        with patch.object(sources, 'CACHE_BYTES', 450):
            for i in range(5):
                sources._cache_put(str(i), {}, [LINK])
            self.assertLessEqual(sum(v[3] for v in sources._CACHE.values()), 450)
            self.assertLess(len(sources._CACHE), 5)

    async def test_bad_parser_does_not_abort_cycle(self):
        with patch.object(sources, 'PublicResolver', LocalResolver), patch.object(sources, 'dedup_key', side_effect=RuntimeError('bad config')):
            self.assertEqual(await sources.collect([self.base + 'good']), [])


class DashboardSourcesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(state, 'STATE_DIR', Path(self.tmp.name)),
                        patch.object(dashboard.cfgmod, 'load_lenient', return_value=({'sources': ['https://example.com/builtin']}, None))]
        for p in self.patches:
            p.start()
        self.client = TestClient(TestServer(dashboard.build_app()))
        await self.client.start_server()
        dashboard._sessions['test-session'] = time.time() + 60
        self.headers = {'Cookie': dashboard.SESSION_COOKIE + '=test-session'}

    async def asyncTearDown(self):
        await self.client.close()
        dashboard._sessions.pop('test-session', None)
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    async def test_requires_login(self):
        for method in ['get', 'post']:
            response = await getattr(self.client, method)('/api/sources')
            self.assertEqual(response.status, 404)

    async def test_add_duplicate_list_and_remove_all_source_types(self):
        for url in ['https://EXAMPLE.com:443/new#x', 'https://example.com/new']:
            response = await self.client.post('/api/sources', json={'url': url}, headers=self.headers)
            self.assertEqual(response.status, 200)
        self.assertEqual(source_store.read_custom(), ['https://example.com/new'])
        response = await self.client.get('/api/sources', headers=self.headers)
        payload = await response.json()
        self.assertEqual(len(payload['sources']), 2)
        for item in payload['sources']:
            response = await self.client.post('/api/sources/remove', json={'id': item['id']}, headers=self.headers)
            self.assertEqual(response.status, 200)
        self.assertEqual(source_store.read_custom(), [])
        self.assertEqual((await (await self.client.get('/api/sources', headers=self.headers)).json())['sources'], [])
        self.assertEqual(source_store.read_store()[1], ['https://example.com/builtin'])

    async def test_edit_builtin_persists_without_touching_config_and_can_restore(self):
        old = 'https://example.com/builtin'
        response = await self.client.post('/api/sources/edit', json={'id': source_store.source_id(old), 'url': 'https://example.com/edited'}, headers=self.headers)
        self.assertEqual(response.status, 200)
        custom, excluded = source_store.read_store()
        self.assertEqual(source_store.effective([old], custom, excluded), ['https://example.com/edited'])
        response = await self.client.post('/api/sources', json={'url': old}, headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(source_store.read_store()[1], [])

    async def test_edit_invalid_duplicate_or_stale_keeps_original(self):
        await self.client.post('/api/sources', json={'url': 'https://example.com/custom'}, headers=self.headers)
        old = source_store.read_store()
        for url in ['http://127.0.0.1/', 'https://example.com/builtin', None]:
            response = await self.client.post('/api/sources/edit', json={'id': source_store.source_id('https://example.com/custom'), 'url': url}, headers=self.headers)
            self.assertEqual(response.status, 400)
            self.assertEqual(source_store.read_store(), old)
        response = await self.client.post('/api/sources/edit', json={'id': 'stale', 'url': 'https://example.com/next'}, headers=self.headers)
        self.assertEqual(response.status, 400)
        self.assertEqual(source_store.read_store(), old)

    async def test_custom_edit_same_url_and_successful_replacement(self):
        original = 'https://example.com/a'
        await self.client.post('/api/sources', json={'url': original}, headers=self.headers)
        for url in [original, 'https://example.com/b']:
            response = await self.client.post('/api/sources/edit', json={'id': source_store.source_id(original), 'url': url}, headers=self.headers)
            self.assertEqual(response.status, 200)
            self.assertEqual(source_store.read_custom(), [url])

    async def test_failed_builtin_edit_is_atomic(self):
        source_store.write_custom([])
        before = (Path(self.tmp.name) / 'sources.json').read_bytes()
        with patch.object(source_store.os, 'replace', side_effect=OSError('disk full')):
            response = await self.client.post('/api/sources/edit', json={'id': source_store.source_id('https://example.com/builtin'), 'url': 'https://example.com/new'}, headers=self.headers)
        self.assertEqual(response.status, 503)
        self.assertEqual((Path(self.tmp.name) / 'sources.json').read_bytes(), before)

    async def test_legacy_sources_migrate_and_concurrent_edits_detect_stale(self):
        url = 'https://example.com/old'
        (Path(self.tmp.name) / 'sources.json').write_text(json.dumps([url]))
        results = await asyncio.gather(*[self.client.post('/api/sources/edit', json={'id': source_store.source_id(url), 'url': f'https://example.com/edit{i}'}, headers=self.headers) for i in range(2)])
        self.assertEqual(sorted(r.status for r in results), [200, 400])
        self.assertEqual(len(source_store.read_custom()), 1)

    async def test_concurrent_additions_not_lost(self):
        results = await asyncio.gather(*[self.client.post('/api/sources', json={'url': f'https://example.com/{i}'}, headers=self.headers) for i in range(8)])
        self.assertTrue(all(r.status == 200 for r in results))
        self.assertEqual(len(source_store.read_custom()), 8)

    async def test_invalid_requests(self):
        for payload in [[], {'url': 'http://localhost/'}, {'url': []}, {'id': 'x'}]:
            response = await self.client.post('/api/sources', json=payload, headers=self.headers)
            self.assertEqual(response.status, 400)
        response = await self.client.post('/api/sources', data='url=https://example.com/x', headers=self.headers)
        self.assertEqual(response.status, 415)
        response = await self.client.post('/api/sources', data='x' * 9000, headers={**self.headers, 'Content-Type': 'application/json'})
        self.assertEqual(response.status, 413)

    async def test_corrupt_state_not_overwritten(self):
        path = Path(self.tmp.name) / 'sources.json'
        path.write_text('{broken')
        response = await self.client.post('/api/sources', json={'url': 'https://example.com/new'}, headers=self.headers)
        self.assertEqual(response.status, 400)
        self.assertEqual(path.read_text(), '{broken')

    async def test_limit(self):
        with patch.object(source_store, 'MAX_CUSTOM', 0):
            response = await self.client.post('/api/sources', json={'url': 'https://example.com/new'}, headers=self.headers)
        self.assertEqual(response.status, 400)


if __name__ == '__main__':
    unittest.main()
