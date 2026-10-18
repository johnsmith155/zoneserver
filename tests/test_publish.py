import unittest
from types import SimpleNamespace
from unittest.mock import patch

import requests
from zonevpn.gist import update_gist


class PublishTests(unittest.TestCase):
    def publish(self, outcomes):
        with patch('zonevpn.gist.requests.patch', side_effect=outcomes) as request, \
                patch('zonevpn.gist.time.sleep'):
            result = update_gist('test-token', 'test-gist', 'list.json', 'same-signed-content')
        return result, request

    def test_lost_response_retries_identical_content(self):
        result, request = self.publish([
            requests.ConnectionError('lost response'), SimpleNamespace(status_code=200)])
        self.assertTrue(result)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0], request.call_args_list[1])

    def test_unreachable_origin_returns_false_instead_of_aborting_cycle(self):
        result, request = self.publish([requests.Timeout(), requests.ConnectionError()])
        self.assertFalse(result)
        self.assertEqual(request.call_count, 2)

    def test_temporary_server_error_is_retried(self):
        result, request = self.publish([
            SimpleNamespace(status_code=503), SimpleNamespace(status_code=200)])
        self.assertTrue(result)
        self.assertEqual(request.call_count, 2)

    def test_auth_and_rate_limit_failures_are_not_retried(self):
        for status in (401, 403, 404, 429):
            with self.subTest(status=status):
                result, request = self.publish([SimpleNamespace(status_code=status)])
                self.assertFalse(result)
                self.assertEqual(request.call_count, 1)


if __name__ == '__main__':
    unittest.main()
