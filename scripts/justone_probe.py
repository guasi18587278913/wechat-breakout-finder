#!/usr/bin/env python3
"""Optional paid account-search probe. Disabled in the main workflow."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import quote, urlencode

from probe import ProbeError, atomic_json, digest, lock, money, now, read_json, redact

ENDPOINT = "https://api.justoneapi.com/api/weixin/search-account/v1"


def transport(params, token):
    url = ENDPOINT + "?" + urlencode(dict(params, token=token))
    config = 'url = ' + json.dumps(url) + '\nrequest = "POST"\n'
    try:
        response = subprocess.run(["curl", "--disable", "--config", "-", "--silent", "--show-error",
            "--max-time", "120", "--max-filesize", "2097152", "--proto", "=https", "--write-out", "\n%{http_code}"],
            input=config.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=125)
    except (OSError, subprocess.TimeoutExpired):
        raise ProbeError("Just One API 网络请求失败，不自动重试。") from None
    body, _, status = response.stdout.rpartition(b"\n")
    if response.returncode or status != b"200" or len(body) > 2097152:
        raise ProbeError("Just One API 未返回正常 HTTP 响应，不自动重试。")
    payload = json.loads(body)
    return redact(redact(payload, token), quote(token, safe=""))


def inspect_quality(payload, expected_biz):
    if payload.get("code") != 0:
        return {"usable": False, "stop_reason": "供应商返回失败"}
    rows = (payload.get("data") or {}).get("data")
    if not isinstance(rows, list) or len(rows) != 1:
        return {"usable": False, "stop_reason": "账号结果为空或结构变化"}
    row = rows[0]
    safe = {k: row.get(k) for k in ("name", "biz", "ghid", "fans", "avg_top_read", "lastest_publish")}
    safe.update(biz_matched=row.get("biz") == expected_biz, usable=False)
    if not safe["biz_matched"]:
        safe["stop_reason"] = "返回账号与目标 biz 不匹配"
    else:
        # lastest_publish is latest post, not when the statistics were measured.
        safe["stop_reason"] = "粉丝数来源及统计更新时间未经核实，不能用于小号认定"
    return safe


def initialize(run, unit_cny, budget_cny, consent, price_source):
    if not consent.strip() or not price_source.startswith("https://"):
        raise ProbeError("需记录用户单独授权和已核实单价的来源。")
    unit, budget = money(unit_cny), money(budget_cny)
    if unit <= 0 or budget < unit:
        raise ProbeError("单价需为正且预算至少覆盖一次请求。")
    with lock(run):
        if (run / "justone-plan.json").exists():
            raise ProbeError("独立探针计划已存在，不得重置预算。")
        plan = {"approved": True, "max_requests": 2, "unit_cny": str(unit), "budget_cny": str(budget),
                "consent": consent, "price_source": price_source, "price_checked_at": now()}
        atomic_json(run / "justone-plan.json", plan)
        atomic_json(run / "justone-authorization.json", {"plan_hash": digest(plan)})
    return {"plan": plan, "note": "每次都按全价预留，不假定有免费额度；质量不合格立即停止。"}


def probe(run, keyword, expected_biz, allow_paid=False, token=None, sender=transport):
    plan = read_json(run / "justone-plan.json")
    if not allow_paid or plan.get("approved") is not True:
        raise ProbeError("Just One API 未单独授权。")
    authorization = run / "justone-authorization.json"
    if not authorization.is_file() or read_json(authorization).get("plan_hash") != digest(plan):
        raise ProbeError("Just One API 计划已偏离原授权。")
    if not keyword.strip() or not expected_biz.strip():
        raise ProbeError("须提供关键词和从已知文章取得的目标 biz。")
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(plan["price_checked_at"])).total_seconds()
    if not 0 <= age <= 86400:
        raise ProbeError("单价记录超过一天，请重新核实，不沿用历史 0.40 元报价。")
    token = token if token is not None else os.environ.get("JUSTONE_API_TOKEN", "")
    if not token or any(ord(c) <= 32 for c in token):
        raise ProbeError("未配置 JUSTONE_API_TOKEN，不要把密钥发到聊天。")
    with lock(run):
        path = run / "justone-ledger.json"
        ledger = read_json(path) if path.exists() else {"plan_hash": digest(plan), "attempts": []}
        if ledger["plan_hash"] != digest(plan):
            raise ProbeError("独立探针计划发生改变。")
        params = {"keyword": keyword, "page": 1}
        for previous in ledger["attempts"]:
            if previous["params"] == params and previous["expected_biz"] == expected_biz and "quality" in previous:
                return {"cache_hit": True, "quality": previous["quality"]}
        if ledger.get("stopped") or any(a["status"] != "success" for a in ledger["attempts"]):
            raise ProbeError("探针已因失败或数据质量停止；不继续扩大付费查询。")
        spent = sum((money(a["reserved_cny"]) for a in ledger["attempts"]), Decimal(0))
        cost = money(plan["unit_cny"])
        if len(ledger["attempts"]) >= min(plan["max_requests"], 2) or spent + cost > money(plan["budget_cny"]):
            raise ProbeError("达到 Just One API 独立次数或人民币预算上限。")
        attempt = {"params": params, "expected_biz": expected_biz, "reserved_cny": str(cost),
                   "started_at": now(), "status": "reserved"}
        ledger["attempts"].append(attempt)
        atomic_json(path, ledger)
        try:
            payload = sender(params, token)
            quality = inspect_quality(redact(payload, token), expected_biz)
            attempt.update(status="success" if payload.get("code") == 0 else "failed", quality=quality)
            ledger["stopped"] = not quality["usable"]
            atomic_json(path, ledger)
        except Exception:
            attempt["status"] = "failed"
            ledger["stopped"] = True
            atomic_json(path, ledger)
            raise ProbeError("探针失败；预留保留，未重试。") from None
    return {"cache_hit": False, "quality": quality, "reserved_cny": str(spent + cost),
            "actual_billing_verified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--run", type=Path, required=True)
    init.add_argument("--unit-cny", required=True)
    init.add_argument("--budget-cny", required=True)
    init.add_argument("--consent", required=True)
    init.add_argument("--price-source", required=True)
    request = sub.add_parser("call")
    request.add_argument("--run", type=Path, required=True)
    request.add_argument("--keyword", required=True)
    request.add_argument("--expected-biz", required=True)
    request.add_argument("--allow-paid", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "init":
            result = initialize(args.run, args.unit_cny, args.budget_cny, args.consent, args.price_source)
        else:
            result = probe(args.run, args.keyword, args.expected_biz, args.allow_paid)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ProbeError, ValueError, KeyError, OSError) as error:
        print(str(error) if isinstance(error, ProbeError) else "探针数据或文件异常，已停止。", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
