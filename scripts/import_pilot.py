#!/usr/bin/env python3
"""Export only an allowlist from the historical pilot. Never reads credentials."""
import argparse
import copy
import hashlib
from pathlib import Path

from analyze import METRICS, article_key, canonical_url, clean, search_candidates
from probe import atomic_json, digest, read_json

ROOT = Path(__file__).resolve().parents[1]


def keep_data(operation, data):
    if operation == "stats":
        return {k: data.get(k) for k in METRICS}
    if operation == "detail":
        content = data["content"]
        fields = ("title", "nick_name", "user_name", "create_timestamp", "idx", "_content_type")
        return {"content": dict({k: content.get(k) for k in fields}, content_text="",
                                body_omitted_for_public_replay=True)}
    if operation == "posts":
        fields = ("app_msg_id", "idx", "title", "create_time", "update_time", "item_show_type")
        return {"biz_username": data["biz_username"], "is_end": data["is_end"],
                "articles": [dict({k: a.get(k) for k in fields}, url=canonical_url(a["url"]))
                             for a in data["articles"]]}
    if operation == "search":
        return {"keyword": data["keyword"], "continue_flag": data["continue_flag"],
                "items": [{"doc_url": canonical_url(i["doc_url"]), "title": clean(i.get("title")),
                           "timestamp": i.get("timestamp"), "source": {"title": i["source"]["title"],
                           "tag": [{"title": str(t.get("title", ""))} for t in i["source"].get("tag", [])]}}
                          for i in data["items"]]}
    raise ValueError("Unsupported operation")


def export(pilot, results, out):
    import json
    if out.exists():
        raise ValueError("Destination exists; do not overwrite frozen evidence")
    raw_ledger = [json.loads(line) for line in (pilot / "ledger.jsonl").read_text().splitlines()]
    completed = {a["id"]: a for a in raw_ledger if a["event"] == "result"}
    plan = read_json(ROOT / "config" / "fde.json")
    plan.update(data_kind="replay", research_date="2026-10-12", created_at="2026-10-12T00:00:00+08:00")
    attempts, records = [], []
    source_hashes = {}
    for original in raw_ledger:
        if original["event"] != "attempt":
            continue
        result = completed.get(original["id"])
        operation = "posts" if original["action"] == "history" else original["action"]
        params = copy.deepcopy(original["params"])
        if "url" in params:
            params["url"] = canonical_url(params["url"])
        attempt = {"operation": operation, "params": params,
                   "fingerprint": digest({"operation": operation, "params": params}),
                   "started_at": original["started_at"], "reserved_usd": str(original["reserved_usd"]),
                   "status": "failed", "attempt_number": 1, "explicit_retry": False}
        if result:
            source = pilot / result["file"]
            payload = read_json(source)
            source_hashes[source.name] = hashlib.sha256(source.read_bytes()).hexdigest()
            if payload.get("code") == 200:
                record = {"operation": operation, "params": params, "fetched_at": original["started_at"],
                          "code": 200, "data": keep_data(operation, payload["data"])}
                attempt.update(status="success", record=record)
                records.append((f"responses/{original['id']:03d}_{operation}.json", record))
            else:
                attempt["error"] = {"kind": "historical_failed_response", "message": "原试采补采失败；未作为数值证据"}
        attempts.append(attempt)
    candidates = search_candidates(records)
    final = read_json(pilot / "verified_final.json")
    final_by_key = {article_key(r["url"]): r for r in final}
    notes = read_json(ROOT / "assets" / "pilot-notes.json")
    analysis = {"schema_version": "editorial-v1", "summary": "原试采留下七篇高表现候选；优先参考获客、入行路径与术语表。",
                "cases": [], "recommendations": []}
    for c in candidates:
        original = final_by_key.get(article_key(c["url"]))
        if original is None:
            c["reason"] = "原试采未进入七篇重点核验；保留搜索线索，不作已验证结论。"
            continue
        c.update(selected=True, reason="原试采人工选定的 FDE 相关候选；复现时固定此选择。",
                 extend_baseline=len(original["baseline"]) > 3)
        analysis["cases"].append(dict(notes[c["account"]], candidate_id=c["id"], evidence_scope="archived_summary"))
        if c["account"] in ("志雄 FDE", "FormulaLab", "FDE部署"):
            analysis["recommendations"].append({"candidate_id": c["id"], "reason": notes[c["account"]]["title_angle"]})
    original_summary = read_json(results / "核验数据.json")
    costs = original_summary["calls"]
    # Public fact table only, never personal balances, tokens, request IDs or raw payloads.
    joa = []
    joa_ledger = [json.loads(line) for line in (pilot / "justone-ledger.jsonl").read_text().splitlines()]
    for entry in joa_ledger:
        if entry["event"] != "result":
            continue
        payload = read_json(pilot / entry["file"])
        data = (payload.get("data") or {}).get("data") or []
        query = entry["params"]["keyword"]
        expected = {article_key(c["url"])[0] for c in candidates if c["account"] == query}
        for record in data:
            joa.append({"query": query, "returned_name": record.get("name"),
                        "biz_matched": record.get("biz") in expected,
                        "lastest_publish": record.get("lastest_publish"),
                        "used_for_small_account_classification": False})
    snapshot = {"format": "public-pilot-snapshot-v1", "plan": plan, "attempts": attempts,
                "candidates": candidates, "analysis": analysis,
                "billing": {"actual_charges_verified": True, "evidence": "原试采余额差与控制台费用记录的精简摘要",
                            "tikhub_usd": str(costs["tikhub"]["actual_balance_change_usd"]),
                            "justone_cny": str(costs["justone"]["actual_charge_cny"]),
                            "justone_requests": costs["justone"]["attempts"],
                            "justone_billed": costs["justone"]["billed_calls"],
                            "justone_free": costs["justone"]["free_calls"]},
                "justone_quality_audit": joa, "source_file_sha256": source_hashes,
                "omitted": ["article full text", "request IDs", "debug fields", "cache URLs", "credentials",
                            "personal account balances", "machine paths", "raw conversation"]}
    out.mkdir(parents=True)
    atomic_json(out / "snapshot.json", snapshot)
    expected = {"search_candidates": 102, "raw_search_rows": 144, "verified_candidates": 7,
                "control_rows": 25, "requests": 50, "successful_requests": 46, "failed_requests": 4,
                "results": {r["account"]: [r["metrics"]["read_num"], r["baseline_median"], r["multiple"]] for r in final}}
    atomic_json(out / "expected.json", expected)
    return {"out": str(out), "attempts": len(attempts), "candidates": len(candidates), "selected": len(final)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot", type=Path, required=True)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(export(args.pilot, args.results, args.out))
