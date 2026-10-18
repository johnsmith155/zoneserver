import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from zonevpn.links import ParsedConfig
from zonevpn.reliability import Reliability
from zonevpn.runner import _apply_reliability
from zonevpn.tester import Tester


def config(address='node.example'):
    return ParsedConfig('vless', address, 443, {}, '')


class Response:
    def __init__(self, clock, delay, status=204):
        self.clock, self.delay, self.status = clock, delay, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def read(self):
        self.clock.now += self.delay / 1000
        return b''


class PingTests(unittest.IsolatedAsyncioTestCase):
    async def measure(self, samples, statistic='median', bad_sample=False):
        clock = SimpleNamespace(now=0.0)
        clock.monotonic = lambda: clock.now
        # A slow warm-up must not affect the published latency.
        responses = [Response(clock, 5000)] + [
            Response(clock, delay, 503 if bad_sample and i == 1 else 204)
            for i, delay in enumerate(samples)
        ]
        session = AsyncMock()
        session.get = unittest.mock.Mock(side_effect=responses)
        session.__aenter__.return_value = session
        tester = Tester('/unused', {
            'ping_samples': len(samples), 'ping_statistic': statistic,
            'max_ping': 1500, 'geo_via_tunnel': False, 'require_censored': False,
        })
        with patch('zonevpn.tester.ProxyConnector.from_url', return_value=AsyncMock()), \
                patch('zonevpn.tester.aiohttp.ClientSession', return_value=session), \
                patch('zonevpn.tester.time', clock):
            return await tester._measure_one(41000, config())

    async def test_median_ignores_single_lucky_response_and_warmup(self):
        result = await self.measure([10, 600, 650])
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.ping, 600, delta=1)

    async def test_legacy_minimum_is_available(self):
        result = await self.measure([10, 600, 650], 'min')
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.ping, 10, delta=1)

    async def test_slow_typical_latency_cannot_pass_on_one_fast_sample(self):
        self.assertIsNone(await self.measure([10, 1800, 1900]))

    async def test_failed_probe_still_rejects_the_server(self):
        self.assertIsNone(await self.measure([10, 20, 30], bad_sample=True))


class ReliabilityTests(unittest.TestCase):
    def test_duplicate_configs_cannot_erase_bad_history_in_one_cycle(self):
        rel = Reliability({'node.example:443': [0, 0, 0, 1, 1]}, window=6)
        tested = [config() for _ in range(3)]
        with patch('zonevpn.runner.Reliability.load', return_value=rel), \
                patch.object(rel, 'save'):
            kept = _apply_reliability({'test': {'min_reliability': 0.6}},
                                      tested, [tested[0]], tested)
        self.assertEqual(kept, [])
        self.assertEqual(rel.samples('node.example:443'), 6)
        self.assertEqual(rel.score('node.example:443'), 0.5)

    def test_duplicate_failures_count_as_one_cycle(self):
        rel = Reliability({})
        tested = [config() for _ in range(4)]
        with patch('zonevpn.runner.Reliability.load', return_value=rel), \
                patch.object(rel, 'save'):
            _apply_reliability({}, tested, [], tested)
        self.assertEqual(rel.samples('node.example:443'), 1)
        self.assertEqual(rel.score('node.example:443'), 0)


if __name__ == '__main__':
    unittest.main()
