#!/usr/bin/env python3
"""Internal bounded TikHub transport; use run.py for authorized workflows.

No paid request without --allow-paid and TIKHUB_API_KEY. Quotes are anonymous.
Each attempt is reserved before sending; errors never trigger automatic retries.
This probe collects evidence, not an automatic claim of a small-account breakout.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import urllib.parse
import urllib.request

BASE = "https://api.tikhub.io"
ENDPOINTS = {
    "search": "/api/v1/wechat_search/v2/fetch_search",
    "detail": "/api/v1/wechat_mp/v2/fetch_article_detail_h5",
    "posts": "/api/v1/wechat_mp/v2/fetch_account_articles",
    "stats": "/api/v1/wechat_mp/v2/fetch_article_stats",
}
PARAMETERS = {
    "search": {"keyword", "business_type", "sort", "publish_time", "offset", "cursor", "raw"},
    "detail": {"url", "raw"},
    "posts": {"username", "page_size", "offset", "item_show_type", "raw"},
    "stats": {"url", "raw"},
}


class ProbeError(Exception):
    def __init__(self, message, diagnostics=None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


def redact(value, key=""):
    if isinstance(value, str):
        return value.replace(key, "[REDACTED]") if key else value
    if isinstance(value, list):
        return [redact(item, key) for item in value]
    if isinstance(value, dict):
        result = {}
        for name, item in value.items():
            normalized = str(name).lower().replace("-", "_")
            result[redact(name, key)] = ("[REDACTED]" if normalized in {
                "authorization", "api_key", "apikey", "access_token", "api_token"
            } else redact(item, key))
        return result
    return value


def read_json(path):
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def lock(run):
    run.mkdir(parents=True, exist_ok=True)
    with (run / ".lock").open("a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0, 2)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ProbeError("同一运行目录有其他任务正在执行。") from None
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def evidence_path(run, relative):
    """Resolve portable evidence paths without following them outside this run."""
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ProbeError("缓存路径必须位于本轮目录内。")
    resolved = (Path(run) / path).resolve()
    try:
        resolved.relative_to(Path(run).resolve())
    except ValueError:
        raise ProbeError("缓存路径越出运行目录。") from None
    return resolved


def load_evidence(run, attempt):
    record = read_json(evidence_path(run, attempt["response_file"]))
    if (record.get("operation") != attempt["operation"]
            or record.get("params") != attempt["params"]
            or digest({"operation": record["operation"], "params": record["params"]}) != attempt["fingerprint"]
            or digest(record) != attempt.get("response_sha256")):
        raise ProbeError("缓存与账本不匹配，停止使用该证据。")
    check_response(record["operation"], record, record["params"])
    return record


def money(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ProbeError("报价或预算不是有效金额。") from None
    if not result.is_finite() or result < 0:
        raise ProbeError("报价或预算不能为负数或非有限数。")
    return result


def fetch_quote(operation):
    path = ENDPOINTS[operation]
    url = BASE + "/api/v1/tikhub/user/get_endpoint_info?" + urllib.parse.urlencode({"endpoint": path})
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    data = payload.get("data", {})
    if payload.get("code") != 200 or data.get("endpoint_uri") != path:
        raise ProbeError("公开报价接口未返回匹配的端点。")
    cost = money(data.get("endpoint_cost"))
    if cost <= 0:
        raise ProbeError("未取得大于零的有效单价，停止。")
    return {"endpoint": path, "unit_usd": str(cost), "checked_at": now(),
            "source": url, "allow_free_credit": data.get("allow_free_credit"),
            "allow_discount": data.get("allow_discount")}


def validate_params(operation, params):
    if operation not in ENDPOINTS or not isinstance(params, dict):
        raise ProbeError("未知操作或参数不是对象。")
    if set(params) - PARAMETERS[operation]:
        raise ProbeError("包含该端点不支持的参数。")
    if operation in ("stats", "detail"):
        url = urllib.parse.urlsplit(str(params.get("url", "")))
        if (url.scheme not in ("http", "https") or url.hostname != "mp.weixin.qq.com"
                or url.username or url.password or url.port not in (None, 80, 443)
                or not (url.path == "/s" or url.path.startswith("/s/"))):
            raise ProbeError("只接受微信公众号文章链接。")
    if operation == "search":
        keyword = params.get("keyword")
        if not isinstance(keyword, str) or not 1 <= len(keyword.strip()) <= 100:
            raise ProbeError("缺少有效搜索关键词。")
        if params.get("business_type", "article") not in ("article", "all", "account"):
            raise ProbeError("本次研究只查询公众号文章与账号。")
    if operation == "posts":
        username = params.get("username")
        if not isinstance(username, str) or not username or len(username) > 80:
            raise ProbeError("缺少有效公众号标识。")
    return params


def send(operation, params, key):
    # Secret goes through stdin, never argv, files, log messages or a redirect.
    options = ["url = " + json.dumps(BASE + ENDPOINTS[operation]), 'request = "POST"',
               'header = "Content-Type: application/json"',
               "header = " + json.dumps("Authorization: Bearer " + key),
               "data-binary = " + json.dumps(json.dumps(params, ensure_ascii=False), ensure_ascii=False)]
    command = ["curl", "--disable", "--config", "-", "--silent", "--show-error", "--max-time", "30",
               "--max-filesize", "8388608", "--proto", "=https", "--write-out", "\n%{http_code}"]
    try:
        result = subprocess.run(command, input=("\n".join(options) + "\n").encode(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=35)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ProbeError("请求未完成；保留费用预留，不自动重试。",
                         {"kind": type(error).__name__}) from None
    if result.returncode:
        raise ProbeError("网络请求失败；保留费用预留，不自动重试。",
                         {"curl_exit_code": result.returncode})
    if len(result.stdout) > 8388613:
        raise ProbeError("响应超过大小限制。", {"kind": "response_too_large"})
    body, _, status = result.stdout.rpartition(b"\n")
    if status != b"200":
        safe_status = status.decode("ascii", errors="replace")[:3]
        raise ProbeError("HTTP " + safe_status + "；已停止，未跟随重定向。",
                         {"http_status": safe_status, "response_excerpt": redact(body.decode("utf-8", errors="replace"), key)[:8192]})
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError):
        raise ProbeError("响应不是有效 JSON；费用预留保留。",
                         {"http_status": "200", "response_excerpt": redact(body.decode("utf-8", errors="replace"), key)[:8192]}) from None
    return payload


def check_response(operation, payload, params=None):
    if not isinstance(payload, dict) or payload.get("code") != 200:
        raise ProbeError("数据服务返回失败，详见本地响应；不自动重试。")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ProbeError("响应缺少 data 对象。")
    for field in ("ret", "status_code"):
        if field in data and data[field] not in (0, "0", None):
            raise ProbeError("上游返回非成功状态；不能作为研究证据。")
    if operation == "stats":
        reads = data.get("read_num")
        # No coercion of unavailable/null or censored counts into a made-up zero.
        if type(reads) is not int or reads < 0:
            raise ProbeError("未取得精确阅读量；保留原始结果，不能判为已核验。")
    elif operation == "search":
        if not isinstance(data.get("items"), list) or "continue_flag" not in data:
            raise ProbeError("搜索字段不符合已核对文档，停止适配。")
    elif operation == "posts":
        if not isinstance(data.get("articles"), list) or "is_end" not in data:
            raise ProbeError("账号文章字段不符合已核对文档，停止适配。")
        if not isinstance(data.get("biz_username"), str) or not data["biz_username"]:
            raise ProbeError("历史列表缺少账号标识，不能记为成功。")
        if params and data["biz_username"] != params.get("username"):
            raise ProbeError("历史返回账号与请求不匹配。")
        for article in data["articles"]:
            if not isinstance(article, dict) or not isinstance(article.get("url"), str):
                raise ProbeError("历史条目缺少文章链接，不能记为成功。")
            validate_params("stats", {"url": article["url"]})
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(article["url"]).query)
            if (not all(query.get(k) for k in ("__biz", "mid", "idx"))
                    or str(article.get("idx", "")) != query["idx"][0]
                    or type(article.get("create_time")) is not int or article["create_time"] <= 0):
                raise ProbeError("历史条目缺少可靠身份、位置或时间字段。")
    else:
        content = data.get("content")
        if (not isinstance(content, dict) or not isinstance(content.get("user_name"), str)
                or not content["user_name"].strip() or not isinstance(content.get("title"), str)
                or type(content.get("create_timestamp")) is not int or content["create_timestamp"] <= 0):
            raise ProbeError("文章详情缺少账号、标题或时间，不能记为成功。")
    return data


def require_authorization(run, plan, allow_paid):
    if plan.get("data_kind") == "replay":
        raise ProbeError("离线回放目录禁止付费取数；新研究请新建目录。")
    if not allow_paid or plan.get("paid_authorization") != "approved":
        raise ProbeError("尚未授权付费。须先由用户确认本轮范围、次数和预算。")
    path = Path(run) / "authorization.json"
    if not path.is_file() or read_json(path).get("plan_hash") != digest(plan):
        raise ProbeError("授权记录缺失或计划已改变；不能沿用旧授权发起请求。")


def call(run, plan, operation, params, allow_paid=False, key=None, sender=send, retry_failed=False):
    require_authorization(run, plan, allow_paid)
    if type(plan.get("max_requests")) is not int or plan["max_requests"] <= 0 or money(plan["budget_usd"]) <= 0:
        raise ProbeError("本轮次数或预算上限无效。")
    params = dict(validate_params(operation, params))
    if params.get("raw", False) is not False:
        raise ProbeError("本方法版本固定 raw=false，不在运行中切换响应结构。")
    params["raw"] = False
    if operation == "search":
        params.setdefault("business_type", "article")
    fingerprint = digest({"operation": operation, "params": params})
    run = Path(run)
    with lock(run):
        state_file = run / "ledger.json"
        state = read_json(state_file) if state_file.exists() else {"plan_hash": digest(plan), "attempts": []}
        if state["plan_hash"] != digest(plan):
            raise ProbeError("运行计划已变化；不能通过修改预算复用本轮账本。")
        prior = [a for a in state["attempts"] if a["fingerprint"] == fingerprint]
        if prior:
            latest = prior[-1]
            if latest["status"] == "success":
                cached = load_evidence(run, latest)
                safe_data = redact(cached["data"], key or os.environ.get("TIKHUB_API_KEY", ""))
                return {"cache_hit": True, "response_file": latest["response_file"], "data": safe_data}
            if not retry_failed or latest["status"] != "failed" or len(prior) >= 2:
                raise ProbeError("该请求已有失败或未完成记录；仅在用户明确要求时允许重试一次。")
        quotes = read_json(run / "quotes.json")
        quote = quotes[operation]
        if quote["endpoint"] != ENDPOINTS[operation]:
            raise ProbeError("报价端点不匹配。")
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(quote["checked_at"])).total_seconds()
        if not 0 <= age <= 7 * 86400:
            raise ProbeError("报价已过期，需重新核对。")
        cost = money(quote["unit_usd"])
        if cost <= 0:
            raise ProbeError("本轮接口单价无效。")
        reserved = sum((money(a["reserved_usd"]) for a in state["attempts"]), Decimal(0))
        if len(state["attempts"]) >= plan["max_requests"] or reserved + cost > money(plan["budget_usd"]):
            raise ProbeError("达到本轮次数或预算上限。")
        key = key if key is not None else os.environ.get("TIKHUB_API_KEY", "")
        if not key or any(ord(char) <= 32 or ord(char) > 126 for char in key):
            raise ProbeError("未配置有效 TIKHUB_API_KEY；不要把 Key 发进聊天。")
        attempt = {"fingerprint": fingerprint, "operation": operation, "params": params,
                   "started_at": now(), "reserved_usd": str(cost), "status": "reserved",
                   "attempt_number": len(prior) + 1, "explicit_retry": bool(prior)}
        state["attempts"].append(attempt)
        atomic_json(state_file, state)
        try:
            payload = sender(operation, params, key)
            suffix = "" if not prior else "-attempt-" + str(len(prior) + 1)
            response_file = run / "responses" / (fingerprint + suffix + ".json")
            # Store needed response evidence only, without request headers or cache URLs.
            evidence = {"operation": operation, "params": params, "fetched_at": now(),
                        "code": payload.get("code") if isinstance(payload, dict) else None,
                        "message": payload.get("message") if isinstance(payload, dict) else None,
                        "request_id": payload.get("request_id") if isinstance(payload, dict) else None,
                        "data": payload.get("data") if isinstance(payload, dict) else None}
            evidence = redact(evidence, key)
            atomic_json(response_file, evidence)
            relative_file = response_file.relative_to(run).as_posix()
            attempt["response_file"] = relative_file
            attempt["response_sha256"] = digest(evidence)
            data = check_response(operation, evidence, params)
            attempt.update(status="success", finished_at=now())
            atomic_json(state_file, state)
        except Exception as error:
            attempt.update(status="failed", finished_at=now())
            attempt["error"] = redact({
                "kind": type(error).__name__,
                "message": str(error) if isinstance(error, ProbeError) else "本地处理失败，原费用预留保留。",
                "diagnostics": getattr(error, "diagnostics", {})
            }, key)
            atomic_json(state_file, state)
            raise
        return {"cache_hit": False, "response_file": relative_file,
                "requests_attempted": len(state["attempts"]), "reserved_usd": str(reserved + cost), "data": data}


if __name__ == "__main__":
    raise SystemExit("此文件是内部传输模块；使用 scripts/run.py 的受控入口。")
