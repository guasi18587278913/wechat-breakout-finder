"""Offline calculations for the 2026-10-12, three-control pilot protocol."""
import html
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from urllib.parse import parse_qs, urlencode, urlsplit

from probe import ProbeError, atomic_json, digest, load_evidence, read_json, validate_params

TZ = timezone(timedelta(hours=8))
METHOD = "recent-three-controls-v1"
METRICS = ("read_num", "like_count", "old_like_count", "share_count", "collect_count", "comment_count")


def article_key(url):
    validate_params("stats", {"url": url})
    query = parse_qs(urlsplit(url).query)
    if all(query.get(k) for k in ("__biz", "mid", "idx")):
        return tuple(query[k][0] for k in ("__biz", "mid", "idx"))
    raise ProbeError("研究候选需要含 __biz、mid、idx 的长链接；短链先解析详情中的 link。")


def canonical_url(url):
    identity = article_key(url)
    query = dict(zip(("__biz", "mid", "idx"), identity))
    sn = parse_qs(urlsplit(url).query).get("sn")
    if sn:
        query["sn"] = sn[0]
    return "https://mp.weixin.qq.com/s?" + urlencode(query) + "#rd"


def clean(text):
    return html.unescape(re.sub(r"<[^>]+>", "", str(text or "")))


def exact(value):
    return type(value) is int and value >= 0


def load_records(run):
    run = Path(run)
    plan = read_json(run / "plan.json")
    ledger = read_json(run / "ledger.json") if (run / "ledger.json").exists() else {
        "plan_hash": digest(plan), "attempts": []}
    if plan.get("method_version") != METHOD or ledger["plan_hash"] != digest(plan):
        raise ProbeError("方法版本或计划与账本不一致，停止复算。")
    records = [(a["response_file"], load_evidence(run, a)) for a in ledger["attempts"]
               if a["status"] == "success"]
    records.sort(key=lambda item: item[1]["fetched_at"])
    return plan, ledger, records


def search_candidates(records):
    """Search counts are approximate ranking hints, never verified metrics."""
    seen = {}
    for path, record in records:
        if record["operation"] != "search":
            continue
        for item in record["data"]["items"]:
            try:
                url = canonical_url(item.get("doc_url", ""))
                identity = article_key(url)
            except (ProbeError, ValueError):
                continue
            source = item.get("source") or {}
            tags = [str(t.get("title", "")) for t in source.get("tag", [])]
            display = next((t for t in tags if "阅读" in t), "")
            match = re.search(r"阅读\s*([\d.]+)(万?)", display)
            try:
                approx = float(match[1]) * (10000 if match[2] else 1) if match else None
            except ValueError:
                approx = None
            if identity not in seen:
                seen[identity] = {"id": digest(identity)[:16], "url": url,
                    "title": clean(item.get("title")), "account": clean(source.get("title")),
                    "timestamp": item.get("timestamp", item.get("date")),
                    "description": clean(item.get("desc")), "read_search": approx,
                    "read_search_text": display, "queries": [], "evidence_files": [],
                    "selected": False, "reason": "", "extend_baseline": False}
            row = seen[identity]
            keyword = record["params"]["keyword"]
            if keyword not in row["queries"]:
                row["queries"].append(keyword)
            if path not in row["evidence_files"]:
                row["evidence_files"].append(path)
    return sorted(seen.values(), key=lambda r: (-(r["read_search"] or 0), r["id"]))


def cutoff(plan):
    # Freeze the original calendar-day convention, not the time of a later rerun.
    anchor = datetime.fromisoformat(plan["research_date"]).replace(tzinfo=TZ)
    return int((anchor - timedelta(days=plan["comparison"]["min_history_age_days"])).timestamp())


def find_record(records, operation, url=None, username=None):
    matches = [(p, r) for p, r in records if r["operation"] == operation
               and (url is None or article_key(r["params"]["url"]) == article_key(url))
               and (username is None or (r["params"].get("username") == username
                    and not r["params"].get("offset")))]
    return matches[-1] if matches else (None, None)


def eligible_history(candidate, records, plan):
    detail_path, detail = find_record(records, "detail", candidate["url"])
    if detail is None:
        return None, None, [], detail_path
    content = detail["data"]["content"]
    username = content.get("user_name")
    if not username:
        raise ProbeError("详情缺少账号标识。")
    history_path, history = find_record(records, "posts", username=username)
    if history is None:
        return content, None, [], detail_path
    data = history["data"]
    if data.get("biz_username") != username:
        raise ProbeError("历史返回的账号与详情不匹配。")
    target = article_key(candidate["url"])
    articles, seen = [], set()
    for item in data["articles"]:
        identity = article_key(item["url"])
        if identity[0] != target[0]:
            raise ProbeError("历史列表含其他公众号文章，停止使用该页。")
        timestamp = item.get("create_time")
        if (identity == target or identity in seen or str(item.get("idx")) != "1"
                or type(timestamp) is not int or timestamp <= 0 or timestamp > cutoff(plan)):
            continue
        seen.add(identity)
        articles.append({"title": clean(item.get("title")), "url": canonical_url(item["url"]),
                         "timestamp": timestamp, "history_evidence": history_path})
    articles.sort(key=lambda a: (-a["timestamp"], article_key(a["url"])))
    return content, history_path, articles, detail_path


def compare(candidate, records, ledger, plan):
    rules = plan["comparison"]
    content, history_path, eligible, detail_path = eligible_history(candidate, records, plan)
    target_path, stats = find_record(records, "stats", candidate["url"])
    metrics = {k: stats["data"].get(k) if stats else None for k in METRICS}
    initial_count = rules["baseline_count"]
    count = initial_count + (rules["extra_baseline_count"] if candidate.get("extend_baseline") else 0)
    controls = []
    for article in eligible[:count]:
        stat_path, record = find_record(records, "stats", article["url"])
        value = record["data"].get("read_num") if record else None
        controls.append(dict(article, read_num=value if exact(value) else None,
                             included=exact(value), stats_evidence=stat_path))
    initial_complete = len(controls) >= initial_count and all(c["included"] for c in controls[:initial_count])
    values = [a["read_num"] for a in controls if a["included"]]
    baseline = median(values) if values else None
    multiple = metrics["read_num"] / baseline if exact(metrics["read_num"]) and baseline else None
    status, reason = "unverified", ""
    if not content or not history_path:
        reason = "缺少成功的详情或首个历史页"
    elif not initial_complete or not exact(metrics["read_num"]):
        reason = "目标或初始三篇对照缺少精确阅读量"
    elif baseline == 0:
        reason = "中位数为零，不能计算可靠倍数"
    elif multiple < rules["min_ratio"]:
        status, reason = "below_threshold", "放大倍数未达到本轮阈值"
    elif baseline < rules["small_baseline_lt"]:
        status = "low_baseline_candidate"
    else:
        status = "supplementary_candidate"
    return {"id": candidate["id"], "account": candidate["account"],
        "title": clean(content.get("title")) if content else candidate["title"],
        "url": canonical_url(candidate["url"]),
        "published_at": content.get("create_timestamp") if content else candidate.get("timestamp"),
        "metrics": metrics, "baseline": controls, "baseline_n": len(values),
        "baseline_median": baseline, "multiple": round(multiple, 2) if multiple is not None else None,
        "status": status, "reason": reason, "detail_evidence": detail_path,
        "target_stats_evidence": target_path,
        "body_available": bool(content and content.get("content_text")),
        "body_sha256": digest(content.get("content_text", "")) if content else None,
        "fans_verified": False, "baseline_time_basis": "history.create_time (creation-time proxy)",
        "method_version": METHOD}


def build(run):
    run = Path(run)
    plan, ledger, records = load_records(run)
    candidates = read_json(run / "candidates.json")
    selected = [c for c in candidates if c.get("selected") is True]
    if len(selected) > plan["max_candidates"]:
        raise ProbeError("候选数超过本轮计划；不能静默扩大取数范围。")
    validate_selection(selected, records)
    result = [compare(c, records, ledger, plan) for c in selected]
    result.sort(key=lambda r: -(r["multiple"] or 0))
    atomic_json(run / "comparison.json", result)
    return result


def validate_selection(selected, records):
    observed = {c["id"]: c for c in search_candidates(records)}
    accounts = set()
    for candidate in selected:
        original = observed.get(candidate.get("id"))
        if (not original or article_key(original["url"]) != article_key(candidate["url"])
                or original["account"] != candidate["account"] or not candidate.get("reason")):
            raise ProbeError("候选与已记录搜索证据不匹配，或缺少复核理由。")
        identity = article_key(candidate["url"])
        if identity[2] != "1" or identity[0] in accounts:
            raise ProbeError("初轮只核验头条且每账号一篇。")
        accounts.add(identity[0])
