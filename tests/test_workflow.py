import copy
import csv
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from analyze import article_key, build, canonical_url, load_records
from justone_probe import initialize as initialize_joa, probe as probe_joa
from probe import ENDPOINTS, ProbeError, atomic_json, call, digest, evidence_path, load_evidence, now, read_json
from report import checked_notes, render, write_csv
from run import query, replay, retry_request, verify


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name) / "study"
        with patch("socket.socket", side_effect=AssertionError("offline")), patch("subprocess.run", side_effect=AssertionError("offline")):
            self.result = replay(self.run)

    def test_original_seven_case_regression_and_counts_without_network(self):
        result = read_json(self.run / "核验数据.json")
        self.assertEqual((result["raw_search_rows"], result["unique_candidate_articles"], result["verified_candidates"]), (144, 102, 7))
        self.assertEqual((result["low_baseline_candidates"], result["supplementary_candidates"]), (6, 1))
        self.assertEqual((result["requests"], result["successful_requests"], result["failed_requests"]), (50, 46, 4))
        self.assertEqual(result["billing"]["tikhub_usd"], "0.46")
        self.assertEqual(result["billing"]["justone_cny"], "4.0")
        with (self.run / "对照文章明细.csv").open(encoding="utf-8-sig") as handle:
            controls = list(csv.DictReader(handle))
        self.assertEqual(len(controls), 25)
        self.assertEqual(sum(c["计入中位数"] == "True" for c in controls), 21)

    def test_high_control_is_not_discarded_and_low_target_is_included(self):
        rows = {r["account"]: r for r in build(self.run)}
        self.assertIn(11359, [b["read_num"] for b in rows["FDE前线"]["baseline"]])
        self.assertEqual(rows["志雄 FDE"]["metrics"]["read_num"], 7081)
        self.assertEqual(rows["志雄 FDE"]["status"], "low_baseline_candidate")
        self.assertEqual(rows["PM杨堃"]["status"], "supplementary_candidate")

    def test_failed_initial_control_does_not_get_replaced_or_zero_filled(self):
        row = build(self.run)[0]
        evidence = row["baseline"][0]["stats_evidence"]
        ledger = read_json(self.run / "ledger.json")
        for attempt in ledger["attempts"]:
            if attempt.get("response_file") == evidence:
                attempt["status"] = "failed"
        atomic_json(self.run / "ledger.json", ledger)
        changed = next(r for r in build(self.run) if r["id"] == row["id"])
        self.assertEqual(changed["status"], "unverified")
        self.assertIsNone(changed["baseline"][0]["read_num"])

    def test_tampered_cache_is_rejected(self):
        ledger = read_json(self.run / "ledger.json")
        entry = next(a for a in ledger["attempts"] if a["status"] == "success" and a["operation"] == "stats")
        record = read_json(self.run / entry["response_file"])
        record["data"]["read_num"] = 9999999
        atomic_json(self.run / entry["response_file"], record)
        with self.assertRaises(ProbeError):
            load_records(self.run)

    def test_wrong_cached_operation_is_rejected_even_with_new_hash(self):
        ledger = read_json(self.run / "ledger.json")
        entry = next(a for a in ledger["attempts"] if a["status"] == "success")
        record = read_json(self.run / entry["response_file"])
        record["params"]["keyword"] = "unrelated"
        atomic_json(self.run / entry["response_file"], record)
        entry["response_sha256"] = digest(record)
        with self.assertRaises(ProbeError):
            load_evidence(self.run, entry)

    def test_move_run_does_not_depend_on_original_absolute_paths(self):
        destination = Path(self.temp.name) / "moved"
        shutil.copytree(self.run, destination)
        shutil.rmtree(self.run)
        self.assertEqual(render(destination)["verified_candidates"], 7)

    def test_path_escape_and_symlink_escape_are_rejected(self):
        for name in ("../x", "/tmp/x"):
            with self.assertRaises(ProbeError):
                evidence_path(self.run, name)
        (self.run / "escape").symlink_to(Path(self.temp.name), target_is_directory=True)
        with self.assertRaises(ProbeError):
            evidence_path(self.run, "escape/secret")

    def test_fake_candidate_cannot_trigger_network(self):
        candidates = read_json(self.run / "candidates.json")
        row = next(r for r in candidates if r["selected"])
        row["url"] = row["url"].replace("mid=", "mid=999")
        atomic_json(self.run / "candidates.json", candidates)
        with patch("run.query") as sender, self.assertRaises(ProbeError):
            verify(self.run, True, "synthetic")
        sender.assert_not_called()

    def test_replay_cannot_start_paid_verification(self):
        with patch("probe.send") as sender, self.assertRaises(ProbeError):
            verify(self.run, True, "synthetic")
        sender.assert_not_called()

    def test_claimed_body_read_requires_real_body(self):
        notes = read_json(self.run / "analysis.json")
        notes["cases"][0]["evidence_scope"] = "full_text"
        atomic_json(self.run / "analysis.json", notes)
        with self.assertRaises(ProbeError):
            render(self.run)

    def test_report_keeps_editorial_summary_recommendations_and_stage(self):
        analysis = read_json(self.run / "analysis.json")
        analysis["summary"] = "唯一总结样本"
        analysis["recommendations"][0]["reason"] = "唯一推荐理由样本"
        atomic_json(self.run / "analysis.json", analysis)
        render(self.run)
        document = (self.run / "report.html").read_text()
        self.assertIn("唯一总结样本", document)
        self.assertIn("唯一推荐理由样本", document)
        self.assertIn("46 成功、4 失败", document)
        self.assertIn("历史分析摘要；回放未包含全文", document)
        plan = read_json(self.run / "plan.json")
        plan["data_kind"] = "live"
        for note in analysis["cases"]:
            note.update(evidence_scope="metadata_only", body_observation="")
        atomic_json(self.run / "analysis.json", analysis)
        _, status, _, _ = checked_notes(self.run, build(self.run), plan)
        self.assertIn("正文分析未完成", status)

    def test_generic_request_cannot_add_an_unapproved_search(self):
        with patch("run.call") as sender, self.assertRaises(ProbeError):
            retry_request(self.run, "search", {"keyword": "not-in-plan"}, True, "synthetic", True)
        sender.assert_not_called()
        with patch("run.call") as sender, self.assertRaises(ProbeError):
            query(self.run, "search", {"keyword": "not-in-plan", "sort": "hot", "publish_time": "half_year"}, True, "synthetic")
        sender.assert_not_called()

    def test_supplement_choice_is_frozen_even_when_extra_reads_change_ranks(self):
        original_rows = build(self.run)
        first_three = original_rows[:3]
        picked = {r["id"] for r in first_three}
        candidates = read_json(self.run / "candidates.json")
        for c in candidates:
            c["selected"] = c["id"] in picked
            c["extend_baseline"] = False
        atomic_json(self.run / "candidates.json", candidates)
        plan = read_json(self.run / "plan.json")
        plan.update(data_kind="live", paid_authorization="approved", max_requests=80, budget_usd="0.80")
        atomic_json(self.run / "plan.json", plan)
        atomic_json(self.run / "authorization.json", {"plan_hash": digest(plan)})
        atomic_json(self.run / "quotes.json", {op: {"endpoint": path, "unit_usd": "0.01", "checked_at": now()}
                                               for op, path in ENDPOINTS.items()})
        ledger = read_json(self.run / "ledger.json")
        ledger["plan_hash"] = digest(plan)
        ledger["attempts"] = [a for a in ledger["attempts"] if a["status"] == "success"]
        edits = {}
        for index, row in enumerate(first_three):
            edits[row["target_stats_evidence"]] = 1000 - index * 100
            for control, value in zip(row["baseline"][:3], [1, 1, 1000] if index < 2 else [2, 2, 2]):
                edits[control["stats_evidence"]] = value
        for entry in ledger["attempts"]:
            path = entry["response_file"]
            if path in edits:
                record = read_json(self.run / path)
                record["data"]["read_num"] = edits[path]
                atomic_json(self.run / path, record)
                entry["response_sha256"] = digest(record)
        atomic_json(self.run / "ledger.json", ledger)
        sender = Mock(return_value={"code": 200, "data": {"read_num": 100000}})
        def bounded_call(*args, **kwargs):
            return call(*args, **kwargs, sender=sender)
        with patch("run.call", side_effect=bounded_call):
            verify(self.run, True, "synthetic")
            self.assertEqual(sender.call_count, 4)
            locked = read_json(self.run / "supplement-selection.json")["candidate_ids"]
            self.assertEqual(set(locked), {r["id"] for r in first_three[:2]})
            verify(self.run, True, "synthetic")
            self.assertEqual(sender.call_count, 4)
        marked = [r["id"] for r in read_json(self.run / "candidates.json") if r["extend_baseline"]]
        self.assertEqual(set(marked), set(locked))

    def test_html_escapes_remote_text_and_csv_blocks_formula_injection(self):
        notes = read_json(self.run / "analysis.json")
        notes["cases"][0]["idea"] = '<script>alert("x")</script>'
        atomic_json(self.run / "analysis.json", notes)
        render(self.run)
        document = (self.run / "report.html").read_text()
        self.assertNotIn('<script>', document)
        self.assertIn('&lt;script&gt;', document)
        path = self.run / "formula.csv"
        write_csv(path, [{"title": "=SUM(1,2)"}], ["title"])
        with path.open(encoding="utf-8-sig") as handle:
            self.assertTrue(next(csv.DictReader(handle))["title"].startswith("'="))

    def test_url_identity_ignores_tracking_and_decodes_biz(self):
        one = "http://mp.weixin.qq.com/s?__biz=abc%3D%3D&mid=123&idx=1&sn=xx&chksm=irrelevant#rd"
        two = "https://mp.weixin.qq.com/s?__biz=abc==&mid=123&idx=1&sn=xx"
        self.assertEqual(article_key(one), article_key(two))
        self.assertNotIn("chksm", canonical_url(one))
        with self.assertRaises(ProbeError):
            article_key("https://mp.weixin.qq.com/s/short")


class JustOneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name)
        initialize_joa(self.run, "0.4", "0.8", "synthetic approval", "https://example.test/quote")

    def test_no_authorization_never_calls_provider(self):
        sender = Mock()
        with self.assertRaises(ProbeError):
            probe_joa(self.run, "name", "biz", False, "synthetic", sender)
        sender.assert_not_called()

    def test_changed_justone_budget_does_not_inherit_original_consent(self):
        plan = read_json(self.run / "justone-plan.json")
        plan["budget_cny"] = "100"
        atomic_json(self.run / "justone-plan.json", plan)
        sender = Mock()
        with self.assertRaises(ProbeError):
            probe_joa(self.run, "name", "biz", True, "synthetic", sender)
        sender.assert_not_called()

    def test_mismatch_stops_additional_paid_calls_and_reserves_full_price(self):
        sender = Mock(return_value={"code": 0, "data": {"data": [{"name": "wrong", "biz": "other", "fans": 99}]}})
        result = probe_joa(self.run, "name", "biz", True, "synthetic", sender)
        self.assertFalse(result["quality"]["usable"])
        self.assertEqual(result["reserved_cny"], "0.4")
        with self.assertRaises(ProbeError):
            probe_joa(self.run, "second", "biz2", True, "synthetic", sender)
        self.assertEqual(sender.call_count, 1)
        self.assertTrue(probe_joa(self.run, "name", "biz", True, "synthetic", sender)["cache_hit"])
        self.assertEqual(sender.call_count, 1)

    def test_matching_biz_does_not_prove_fan_freshness(self):
        sender = Mock(return_value={"code": 0, "data": {"data": [{"biz": "biz", "fans": 55, "lastest_publish": 1739181600}]}})
        result = probe_joa(self.run, "name", "biz", True, "synthetic", sender)
        self.assertTrue(result["quality"]["biz_matched"])
        self.assertFalse(result["quality"]["usable"])


if __name__ == "__main__":
    unittest.main()
