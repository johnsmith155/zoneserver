import unittest

from zonevpn.tester import Tester


class Body:
    def __init__(self, length):
        self.length = length

    async def iter_chunked(self, size):
        remaining = self.length
        while remaining:
            chunk = min(size, remaining)
            yield b'x' * chunk
            remaining -= chunk


class Response:
    def __init__(self, status, length):
        self.status = status
        self.content = Body(length)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class Session:
    def __init__(self, status, length):
        self.response = Response(status, length)

    def get(self, *args, **kwargs):
        return self.response


class ThroughputTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tester = Tester('/unused', {'throughput_bytes': 65536})

    async def test_error_page_is_not_a_successful_speed_measurement(self):
        rate = await self.tester._measure_kbps(Session(403, 65536))
        self.assertEqual(rate, 0)

    async def test_short_response_is_not_a_successful_download(self):
        rate = await self.tester._measure_kbps(Session(200, 1024))
        self.assertEqual(rate, 0)

    async def test_complete_download_has_a_positive_rate(self):
        rate = await self.tester._measure_kbps(Session(200, 65536))
        self.assertGreater(rate, 0)

    async def test_complete_partial_content_response_is_supported(self):
        rate = await self.tester._measure_kbps(Session(206, 65536))
        self.assertGreater(rate, 0)


if __name__ == '__main__':
    unittest.main()
