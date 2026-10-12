import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from probe import ENDPOINTS, ProbeError, atomic_json, call, check_response, digest, now, read_json, send


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name)
        self.plan = {"max_requests": 2, "budget_usd": "0.02", "paid_authorization": "approved"}
        atomic_json(self.run / "authorization.json", {"plan_hash": digest(self.plan)})
        self.params = {"url": "https://mp.weixin.qq.com/s/example"}
        self.sender = Mock(return_value={"code": 200, "data": {"read_num": 1234}})
        self.quotes = {op: {"endpoint": path, "unit_usd": "0.01", "checked_at": now()}
                       for op, path in ENDPOINTS.items()}
        atomic_json(self.run / "quotes.json", self.quotes)

    def query(self, params=None, plan=None):
        if plan is not None and not (self.run / "ledger.json").exists():
            atomic_json(self.run / "authorization.json", {"plan_hash": digest(plan)})
        return call(self.run, plan or self.plan, "stats", params or self.params,
                    allow_paid=True, key="offline-test-key", sender=self.sender)

    def test_unapproved_call_never_reaches_network(self):
        with self.assertRaises(ProbeError):
            call(self.run, self.plan, "stats", self.params, sender=self.sender)
        self.sender.assert_not_called()
        self.assertFalse((self.run / "ledger.json").exists())

    def test_missing_key_never_reaches_network(self):
        with self.assertRaises(ProbeError):
            call(self.run, self.plan, "stats", self.params, True, key="", sender=self.sender)
        self.sender.assert_not_called()

    def test_pending_plan_blocks_even_with_allow_paid_flag(self):
        plan = dict(self.plan, paid_authorization="pending")
        with self.assertRaises(ProbeError):
            self.query(plan=plan)
        self.sender.assert_not_called()

    def test_reservation_exists_before_request(self):
        def send(*_):
            ledger = read_json(self.run / "ledger.json")
            self.assertEqual(ledger["attempts"][0]["status"], "reserved")
            self.assertEqual(ledger["attempts"][0]["reserved_usd"], "0.01")
            return {"code": 200, "data": {"read_num": 1234}}
        self.sender.side_effect = send
        self.query()

    def test_success_cache_does_not_charge_twice_or_persist_key(self):
        first = self.query()
        second = self.query()
        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual(self.sender.call_count, 1)
        for path in self.run.rglob("*.json"):
            self.assertNotIn("offline-test-key", path.read_text())

    def test_failure_keeps_reservation_and_prevents_retry(self):
        self.sender.side_effect = ProbeError("offline timeout")
        for _ in range(2):
            with self.assertRaises(ProbeError):
                self.query()
        self.assertEqual(self.sender.call_count, 1)
        row = read_json(self.run / "ledger.json")["attempts"][0]
        self.assertEqual(row["reserved_usd"], "0.01")
        self.assertEqual(row["status"], "failed")

    def test_dollar_cap_prevents_extra_attempt(self):
        plan = dict(self.plan, max_requests=10, budget_usd="0.01")
        self.query(plan=plan)
        with self.assertRaises(ProbeError):
            self.query({"url": "https://mp.weixin.qq.com/s/another"}, plan)
        self.assertEqual(self.sender.call_count, 1)

    def test_request_cap_prevents_extra_attempt(self):
        plan = dict(self.plan, max_requests=1, budget_usd="1.00")
        self.query(plan=plan)
        with self.assertRaises(ProbeError):
            self.query({"url": "https://mp.weixin.qq.com/s/another"}, plan)
        self.assertEqual(self.sender.call_count, 1)

    def test_changed_plan_cannot_reset_budget(self):
        self.query()
        changed = copy.deepcopy(self.plan)
        changed["budget_usd"] = "10.00"
        with self.assertRaises(ProbeError):
            self.query(plan=changed)
        self.assertEqual(self.sender.call_count, 1)

    def test_authorized_plan_change_is_blocked_before_first_request(self):
        changed = dict(self.plan, budget_usd="100.00", keywords=["unapproved"])
        with self.assertRaises(ProbeError):
            call(self.run, changed, "stats", self.params, True, key="offline-test-key", sender=self.sender)
        self.sender.assert_not_called()
        self.assertFalse((self.run / "ledger.json").exists())

    def test_empty_detail_is_failed_then_can_be_explicitly_retried(self):
        self.sender.side_effect = [{"code": 200, "data": {"content": {}}},
            {"code": 200, "data": {"content": {"user_name": "gh_example", "title": "Example", "create_timestamp": 1783130400}}}]
        with self.assertRaises(ProbeError):
            call(self.run, self.plan, "detail", self.params, True, key="offline-test-key", sender=self.sender)
        first = read_json(self.run / "ledger.json")["attempts"][0]
        self.assertEqual(first["status"], "failed")
        result = call(self.run, self.plan, "detail", self.params, True, key="offline-test-key", sender=self.sender, retry_failed=True)
        self.assertFalse(result["cache_hit"])
        self.assertEqual(self.sender.call_count, 2)

    def test_wrong_or_missing_history_identity_does_not_become_success(self):
        for data in ({"articles": [], "is_end": 1},
                     {"biz_username": "other", "articles": [], "is_end": 1},
                     {"biz_username": "gh_example", "articles": [{}], "is_end": 1}):
            with self.subTest(data=data), self.assertRaises(ProbeError):
                check_response("posts", {"code": 200, "data": data}, {"username": "gh_example"})

    def test_missing_censored_boolean_and_negative_reads_not_verified(self):
        for value in (None, "10万+", True, -1, 1.5):
            with self.subTest(value=value), self.assertRaises(ProbeError):
                check_response("stats", {"code": 200, "data": {"read_num": value}})
        self.assertEqual(check_response("stats", {"code": 200, "data": {"read_num": 0}})["read_num"], 0)

    def test_non_wechat_url_never_reaches_network(self):
        for url in ("https://mp.weixin.qq.com.evil.test/s/a", "https://evil.test/s/a",
                    "https://user:pass@mp.weixin.qq.com/s/a", "file:///tmp/example"):
            with self.subTest(url=url), self.assertRaises(ProbeError):
                self.query({"url": url})
        self.sender.assert_not_called()

    def test_expired_quote_stops_before_network(self):
        self.quotes["stats"]["checked_at"] = "2020-01-01T00:00:00+00:00"
        atomic_json(self.run / "quotes.json", self.quotes)
        with self.assertRaises(ProbeError):
            self.query()
        self.sender.assert_not_called()

    def test_upstream_error_not_treated_as_success(self):
        with self.assertRaises(ProbeError):
            check_response("stats", {"code": 200, "data": {"status_code": 1, "read_num": 10}})

    def test_response_key_echo_is_removed_from_disk_and_output(self):
        self.sender.return_value = {"code": 200, "data": {"read_num": 10, "debug": {
            "Authorization": "Bearer offline-test-key", "note": "echo offline-test-key"}}}
        result = self.query()
        self.assertNotIn("offline-test-key", json.dumps(result))
        self.assertNotIn("offline-test-key", json.dumps(self.query()))
        for path in self.run.rglob("*.json"):
            self.assertNotIn("offline-test-key", path.read_text())

    def test_http_failure_preserves_sanitized_diagnostics(self):
        from unittest.mock import patch
        response = Mock(returncode=0, stdout=b'{"error":"echo offline-test-key"}\n429')
        with patch("probe.subprocess.run", return_value=response):
            with self.assertRaises(ProbeError):
                call(self.run, self.plan, "stats", self.params, True, key="offline-test-key", sender=send)
        ledger = read_json(self.run / "ledger.json")
        self.assertEqual(ledger["attempts"][0]["error"]["diagnostics"]["http_status"], "429")
        self.assertNotIn("offline-test-key", json.dumps(ledger))
        self.assertEqual(ledger["attempts"][0]["reserved_usd"], "0.01")

    def test_api_failure_preserves_message_and_request_id(self):
        self.sender.return_value = {"code": 402, "message": "balance error for offline-test-key",
                                    "request_id": "offline-request", "data": None}
        with self.assertRaises(ProbeError):
            self.query()
        response = read_json(next((self.run / "responses").glob("*.json")))
        self.assertEqual(response["request_id"], "offline-request")
        self.assertEqual(response["code"], 402)
        self.assertIn("balance error", response["message"])
        self.assertNotIn("offline-test-key", json.dumps(response))

    def test_diagnostic_truncation_happens_after_redaction(self):
        from unittest.mock import patch
        body = b"x" * 8188 + b"offline-test-key" + b"\n403"
        response = Mock(returncode=0, stdout=body)
        with patch("probe.subprocess.run", return_value=response):
            with self.assertRaises(ProbeError):
                call(self.run, self.plan, "stats", self.params, True, key="offline-test-key", sender=send)
        ledger = read_json(self.run / "ledger.json")
        excerpt = ledger["attempts"][0]["error"]["diagnostics"]["response_excerpt"]
        self.assertEqual(len(excerpt), 8192)
        self.assertTrue(excerpt.endswith("[RED"))

    def test_live_method_rejects_raw_schema_switch_before_network(self):
        with self.assertRaises(ProbeError):
            call(self.run, self.plan, "posts", {"username": "gh_example", "raw": True},
                 True, key="offline-test-key", sender=self.sender)
        self.sender.assert_not_called()

    def test_explicit_retry_preserves_failure_and_uses_remaining_budget(self):
        self.sender.side_effect = [{"code": 500, "message": "offline failure", "data": None},
                                   {"code": 200, "data": {"read_num": 10}}]
        with self.assertRaises(ProbeError):
            self.query()
        result = call(self.run, self.plan, "stats", self.params, True, key="offline-test-key",
                      sender=self.sender, retry_failed=True)
        ledger = read_json(self.run / "ledger.json")
        self.assertEqual([a["status"] for a in ledger["attempts"]], ["failed", "success"])
        self.assertEqual(result["reserved_usd"], "0.02")
        self.assertEqual(len(list((self.run / "responses").glob("*.json"))), 2)
        self.assertTrue(self.query()["cache_hit"])
        self.assertEqual(self.sender.call_count, 2)

    def test_failed_retry_cannot_be_attempted_a_third_time(self):
        self.sender.side_effect = ProbeError("offline failure")
        plan = dict(self.plan, max_requests=10, budget_usd="1.00")
        atomic_json(self.run / "authorization.json", {"plan_hash": digest(plan)})
        for _ in range(3):
            with self.assertRaises(ProbeError):
                call(self.run, plan, "stats", self.params, True, key="offline-test-key",
                     sender=self.sender, retry_failed=True)
        self.assertEqual(self.sender.call_count, 2)

    def test_explicit_retry_does_not_bypass_budget(self):
        self.sender.side_effect = ProbeError("offline failure")
        plan = dict(self.plan, budget_usd="0.01")
        with self.assertRaises(ProbeError):
            self.query(plan=plan)
        with self.assertRaises(ProbeError):
            call(self.run, plan, "stats", self.params, True, key="offline-test-key",
                 sender=self.sender, retry_failed=True)
        self.assertEqual(self.sender.call_count, 1)


if __name__ == "__main__":
    unittest.main()
