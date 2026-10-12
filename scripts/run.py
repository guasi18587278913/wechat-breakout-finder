#!/usr/bin/env python3
"""Portable, agent-assisted WeChat research. No network in replay or report."""
import argparse
import copy
import getpass
import json
import os
from pathlib import Path
import shutil
import sys
from datetime import datetime

from analyze import METHOD, TZ, article_key, build, canonical_url, compare, eligible_history, load_records, search_candidates, validate_selection
from probe import ENDPOINTS, ProbeError, atomic_json, call, digest, fetch_quote, lock, money, now, read_json, require_authorization

ROOT = Path(__file__).resolve().parents[1]


def validate_plan(plan):
    if plan.get("method_version") != METHOD:
        raise ProbeError("不支持的方法版本。")
    try:
        datetime.strptime(plan["research_date"], "%Y-%m-%d")
    except (KeyError, ValueError, TypeError):
        raise ProbeError("研究日期需固定为 YYYY-MM-DD。") from None
    for name in ("max_requests", "max_candidates"):
        if type(plan.get(name)) is not int or plan[name] < 1:
            raise ProbeError("次数和候选上限必须为正整数。")
    if money(plan.get("budget_usd")) <= 0:
        raise ProbeError("美元预算必须大于零。")
    rules = plan.get("comparison", {})
    expected = read_json(ROOT / "config" / "fde.json")["comparison"]
    if rules != expected:
        raise ProbeError("该方法版本的比较口径已固定；更改口径须新增版本和回归样本。")
    keywords = plan.get("keywords")
    if not isinstance(keywords, list) or not keywords or any(not isinstance(x, str) or not x.strip() for x in keywords):
        raise ProbeError("至少需要一个明确关键词。")
    if plan.get("search_sort") != "hot" or plan.get("search_window") != "half_year":
        raise ProbeError("该版本使用近半年、最热搜索；范围变化须先更新研究协议。")


def initialize(out, topic=None, keywords=None):
    out = Path(out)
    if out.exists():
        raise ProbeError("输出目录已存在；请选择新目录，避免覆盖研究状态。")
    plan = read_json(ROOT / "config" / "fde.json")
    if topic:
        plan["topic"] = topic
        if not keywords:
            raise ProbeError("新主题需要 --keyword；由宿主先提出术语、别称和业务表达。")
        plan["keywords"] = keywords
        plan["include"], plan["exclude"] = [], []
    elif keywords:
        plan["keywords"] = keywords
    plan["research_date"] = datetime.now(TZ).date().isoformat()
    plan["created_at"] = now()
    validate_plan(plan)
    out.mkdir(parents=True)
    atomic_json(out / "plan.json", plan)
    atomic_json(out / "candidates.json", [])
    return {"run": str(out.resolve()), "plan": plan, "next": "核对主题与纳排范围，quote 后向用户说明预算。"}


def quote(run):
    plan = read_json(run / "plan.json")
    validate_plan(plan)
    with lock(run):
        if (run / "ledger.json").exists():
            raise ProbeError("已有账本，本轮报价不能改写。")
        quotes = {op: fetch_quote(op) for op in ENDPOINTS}
        atomic_json(run / "quotes.json", quotes)
    return {"quotes": quotes, "max_requests": plan["max_requests"], "budget_usd": plan["budget_usd"]}


def authorize(run, consent):
    if not consent or not consent.strip():
        raise ProbeError("需要记录用户对本轮范围、次数和金额的明确同意；不能替用户同意。")
    with lock(run):
        plan = read_json(run / "plan.json")
        validate_plan(plan)
        if plan.get("data_kind") == "replay" or (run / "ledger.json").exists():
            raise ProbeError("回放或已开始运行的计划不能重新授权改写。")
        if not (run / "quotes.json").is_file():
            raise ProbeError("先获取本轮公开报价。")
        plan.update(paid_authorization="approved", authorization_note=consent, approved_at=now())
        atomic_json(run / "plan.json", plan)
        atomic_json(run / "authorization.json", {"plan_hash": digest(plan), "recorded_at": now()})
    return {"authorized": True, "max_requests": plan["max_requests"], "budget_usd": plan["budget_usd"]}


def query(run, operation, params, allow_paid, key, retry=False):
    plan = read_json(run / "plan.json")
    validate_plan(plan)
    params = dict(params)
    if operation in ("stats", "detail"):
        params["url"] = canonical_url(params["url"])
    if operation == "search" and (params.get("keyword") not in plan["keywords"]
            or params.get("business_type", "article") != "article"
            or params.get("sort") != plan["search_sort"]
            or params.get("publish_time") != plan["search_window"]
            or params.get("offset") or params.get("cursor")):
        raise ProbeError("搜索参数不在本轮授权范围内。")
    return call(run, plan, operation, params, allow_paid=allow_paid, key=key, retry_failed=retry)


def retry_request(run, operation, params, allow_paid, key, explicit_retry):
    if not explicit_retry:
        raise ProbeError("request 仅用于用户明确要求的原失败请求重试，不接受新增查询。")
    _, ledger, _ = load_records(run)
    normalized = dict(params, raw=False)
    if params.get("raw", False) is not False:
        raise ProbeError("本方法不切换 raw 结构。")
    if operation in ("stats", "detail"):
        normalized["url"] = canonical_url(normalized["url"])
    if operation == "search":
        normalized.setdefault("business_type", "article")
    prior = [a for a in ledger["attempts"] if a["operation"] == operation and a["params"] == normalized]
    if not prior or prior[-1]["status"] != "failed":
        raise ProbeError("没有完全匹配的原失败请求，不能追加查询。")
    return query(run, operation, normalized, allow_paid, key, retry=True)


def review(run):
    _, _, records = load_records(run)
    candidates = search_candidates(records)
    previous = read_json(run / "candidates.json")
    by_id = {c["id"]: c for c in previous}
    for row in candidates:
        for field in ("selected", "reason", "extend_baseline"):
            if row["id"] in by_id:
                row[field] = by_id[row["id"]].get(field, row[field])
    atomic_json(run / "candidates.json", candidates)
    return {"unique_candidates": len(candidates), "file": str(run / "candidates.json"),
            "next": "宿主逐条复核相关性，填写 selected/reason；优先每账号一篇，最多七篇。"}


def verify(run, allow_paid, key):
    plan, _, records = load_records(run)
    validate_plan(plan)
    require_authorization(run, plan, allow_paid)
    candidates = read_json(run / "candidates.json")
    selected = [c for c in candidates if c.get("selected") is True]
    if not selected or len(selected) > plan["max_candidates"]:
        raise ProbeError("先复核并选择计划上限内的候选。")
    validate_selection(selected, records)
    selection_hash = digest(sorted(c["id"] for c in selected))
    with lock(run):
        selection_path = run / "verification-selection.json"
        if selection_path.exists():
            if read_json(selection_path)["selection_hash"] != selection_hash:
                raise ProbeError("已开始核验的账号选择已冻结，不能静默替换候选。")
        else:
            atomic_json(selection_path, {"selection_hash": selection_hash,
                                        "candidate_ids": sorted(c["id"] for c in selected)})
    identities, accounts = set(), set()
    for candidate in selected:
        identity = article_key(candidate["url"])
        if not candidate.get("reason") or identity[2] != "1":
            raise ProbeError("每个已选候选必须有理由；本版研究对象是头条文章 idx=1。")
        if identity in identities or identity[0] in accounts:
            raise ProbeError("初轮每个公众号只核验一篇，不重复扣费。")
        identities.add(identity)
        accounts.add(identity[0])
    # A single invocation resumes exact successful requests from the same ledger.
    failures = []
    for candidate in selected:
        try:
            detail = query(run, "detail", {"url": candidate["url"]}, allow_paid, key)
            username = detail["data"]["content"].get("user_name")
            if not username:
                raise ProbeError("详情缺少 user_name，不能核验账号历史。")
            query(run, "posts", {"username": username, "raw": False}, allow_paid, key)
            query(run, "stats", {"url": candidate["url"]}, allow_paid, key)
            _, _, records = load_records(run)
            _, _, eligible, _ = eligible_history(candidate, records, plan)
            for article in eligible[:plan["comparison"]["baseline_count"]]:
                query(run, "stats", {"url": article["url"]}, allow_paid, key)
        except ProbeError as error:
            failures.append({"candidate_id": candidate["id"], "reason": str(error)})
            # Authentication/network/shape/limit failures stop the paid batch.
            # The agent reviews the saved partial result before any explicit resume.
            atomic_json(run / "interruptions.json", failures)
            build(run)
            raise
    plan, ledger, records = load_records(run)
    supplement_path = run / "supplement-selection.json"
    with lock(run):
        if supplement_path.exists():
            supplement = read_json(supplement_path)
            if supplement["selection_hash"] != selection_hash:
                raise ProbeError("补采选择与原候选集合不一致。")
            ordered_ids = supplement["candidate_ids"]
        else:
            rows = [compare(dict(c, extend_baseline=False), records, ledger, plan) for c in selected]
            rows.sort(key=lambda r: (-(r["multiple"] or 0), r["id"]))
            ordered_ids = [r["id"] for r in rows if r["status"] in
                           ("low_baseline_candidate", "supplementary_candidate")][:plan["comparison"]["extra_top_accounts"]]
            atomic_json(supplement_path, {"selection_hash": selection_hash, "candidate_ids": ordered_ids})
    for candidate in candidates:
        candidate["extend_baseline"] = candidate["id"] in ordered_ids
    atomic_json(run / "candidates.json", candidates)
    for candidate in selected:
        if candidate["id"] not in ordered_ids:
            continue
        _, _, records = load_records(run)
        _, _, eligible, _ = eligible_history(candidate, records, plan)
        n = plan["comparison"]["baseline_count"]
        for article in eligible[n:n + plan["comparison"]["extra_baseline_count"]]:
            try:
                query(run, "stats", {"url": article["url"]}, allow_paid, key)
            except ProbeError as error:
                # Extra samples are optional; preserve the valid initial three.
                # Stop on the first failure instead of repeatedly buying bad evidence.
                failures.append({"candidate_id": candidate["id"], "reason": str(error)})
                atomic_json(run / "interruptions.json", failures)
                return {"results": build(run), "supplement_stopped": True, "reason": str(error)}
    return {"results": build(run), "supplement_stopped": False}


def replay(out):
    """Rehydrate whitelisted evidence, using the same analyzers as live runs."""
    if out.exists():
        raise ProbeError("回放输出目录已存在。")
    fixture = read_json(ROOT / "examples" / "fde-20261012" / "snapshot.json")
    out.mkdir(parents=True)
    plan = fixture["plan"]
    atomic_json(out / "plan.json", plan)
    ledger = {"plan_hash": digest(plan), "attempts": []}
    for i, entry in enumerate(fixture["attempts"], 1):
        attempt = copy.deepcopy(entry)
        record = attempt.pop("record", None)
        if record:
            filename = f"responses/{i:03d}_{record['operation']}.json"
            atomic_json(out / filename, record)
            attempt.update(response_file=filename, response_sha256=digest(record))
        ledger["attempts"].append(attempt)
    atomic_json(out / "ledger.json", ledger)
    atomic_json(out / "candidates.json", fixture["candidates"])
    atomic_json(out / "analysis.json", fixture["analysis"])
    atomic_json(out / "billing.json", fixture["billing"])
    from report import render
    result = render(out)
    expected = read_json(ROOT / "examples" / "fde-20261012" / "expected.json")
    actual = {r["account"]: [r["metrics"]["read_num"], r["baseline_median"], r["multiple"]]
              for r in build(out)}
    if actual != expected["results"] or result["search_candidates"] != expected["search_candidates"]:
        raise ProbeError("回归失败：结果与固定样本不一致。")
    return dict(result, replay_checks="passed", paid_requests_made=0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    init = commands.add_parser("init")
    init.add_argument("--out", type=Path, required=True)
    init.add_argument("--topic")
    init.add_argument("--keyword", action="append")
    demo = commands.add_parser("replay")
    demo.add_argument("--out", type=Path, required=True)
    for name in ("quote", "authorize", "search", "review", "verify", "report", "draft-analysis", "request", "status"):
        sub = commands.add_parser(name)
        sub.add_argument("--run", type=Path, required=True)
        if name == "authorize":
            sub.add_argument("--consent", required=True)
        if name in ("search", "verify", "request"):
            sub.add_argument("--allow-paid", action="store_true")
            sub.add_argument("--prompt-key", action="store_true")
        if name == "search":
            sub.add_argument("--keyword", required=True)
        if name == "request":
            sub.add_argument("--operation", choices=tuple(ENDPOINTS), required=True)
            sub.add_argument("--params", type=Path, required=True)
            sub.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args()
    try:
        if sys.version_info < (3, 9):
            raise ProbeError("需要 Python 3.9 或以上。")
        key = os.environ.get("TIKHUB_API_KEY", "")
        if getattr(args, "prompt_key", False):
            key = getpass.getpass("TikHub Key（仅本进程使用，不显示、不保存）: ")
        if args.command == "doctor":
            result = {"python": sys.version.split()[0], "curl": bool(shutil.which("curl")),
                      "third_party_python_dependencies": [], "key_configured": bool(key),
                      "method_version": METHOD, "source_dir": str(ROOT)}
        elif args.command == "init":
            result = initialize(args.out, args.topic, args.keyword)
        elif args.command == "replay":
            result = replay(args.out)
        elif args.command == "quote":
            result = quote(args.run)
        elif args.command == "authorize":
            result = authorize(args.run, args.consent)
        elif args.command == "search":
            plan = read_json(args.run / "plan.json")
            if args.keyword not in plan["keywords"]:
                raise ProbeError("关键词不在已确认计划中。")
            result = query(args.run, "search", {"keyword": args.keyword, "business_type": "article",
                           "sort": plan["search_sort"], "publish_time": plan["search_window"]}, args.allow_paid, key)
        elif args.command == "review":
            result = review(args.run)
        elif args.command == "verify":
            result = verify(args.run, args.allow_paid, key)
        elif args.command == "request":
            result = retry_request(args.run, args.operation, read_json(args.params), args.allow_paid, key, args.retry_failed)
        elif args.command == "status":
            plan, ledger, _ = load_records(args.run)
            from decimal import Decimal
            result = {"attempts": len(ledger["attempts"]), "reserved_usd": str(sum(
                (money(a["reserved_usd"]) for a in ledger["attempts"]), Decimal(0))),
                "budget_usd": plan["budget_usd"], "data_kind": plan["data_kind"]}
        else:
            from report import draft, render
            result = draft(args.run) if args.command == "draft-analysis" else render(args.run)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (ProbeError, OSError, ValueError, KeyError, TypeError) as error:
        print(str(error) if isinstance(error, ProbeError) else "文件、网络或数据格式异常，已停止。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
