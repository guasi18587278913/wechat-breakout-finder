"""CSV, JSON and HTML exports; numeric fields always come from calculations."""
import csv
import html
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from analyze import TZ, build, canonical_url, load_records, search_candidates
from probe import ProbeError, atomic_json, digest, money, read_json

ROOT = Path(__file__).resolve().parents[1]
LABELS = {"low_baseline_candidate": "低日常阅读账号的高表现候选",
          "supplementary_candidate": "补充：日常阅读较高的相对高表现文章",
          "below_threshold": "未达放大阈值", "unverified": "待核验"}
LIMITS = [
    "粉丝规模未可靠确认；低常态阅读是代理，不能证明真实粉丝少。",
    "基线来自接口首个历史页的近期非目标头条文章；初始仅三篇，不代表完整历史平均。",
    "复现原工作流使用 history.create_time 筛选七天前样本；它是创建时间代理，未独立证明真实发布年龄。",
    "目标与对照是采集时累计阅读，文章年龄未归一化；对照可晚于目标。",
    "搜索、历史列表与第三方统计不保证完整覆盖，也没有公众号运营者后台交叉认证。",
]


def day(value):
    return datetime.fromtimestamp(value, TZ).date().isoformat() if type(value) is int and value > 0 else ""


def draft(run):
    path = Path(run) / "analysis.json"
    if path.exists():
        raise ProbeError("分析文件已存在，请保留原稿后编辑。")
    rows = build(run)
    notes = {"schema_version": "editorial-v1", "summary": "", "recommendations": [],
             "cases": [{"candidate_id": r["id"], "evidence_scope": "full_text" if r["body_available"] else "metadata_only",
                        "detail_evidence": r["detail_evidence"], "body_sha256": r["body_sha256"],
                        "title_angle": "", "body_observation": "", "idea": ""} for r in rows]}
    atomic_json(path, notes)
    return {"analysis": str(path), "next": "宿主读取各条成功详情缓存的 content_text 后填写；不得用标题编造正文。"}


def checked_notes(run, rows, plan):
    path = Path(run) / "analysis.json"
    if not path.exists():
        return {}, "正文分析待补充", [], ""
    analysis = read_json(path)
    if analysis.get("schema_version") != "editorial-v1":
        raise ProbeError("不支持的分析格式。")
    by_id = {r["id"]: r for r in rows}
    notes = {}
    for note in analysis.get("cases", []):
        identity = note.get("candidate_id")
        if identity not in by_id or identity in notes:
            raise ProbeError("分析引用了未知或重复候选。")
        row = by_id[identity]
        scope = note.get("evidence_scope")
        if scope == "full_text":
            if (not row["body_available"] or note.get("body_sha256") != row["body_sha256"]
                    or note.get("detail_evidence") != row["detail_evidence"]):
                raise ProbeError("正文分析未绑定当前已读取的内容证据。")
        elif scope == "archived_summary":
            if plan["data_kind"] != "replay":
                raise ProbeError("原会话摘要仅用于历史回放。")
        elif scope != "metadata_only" or note.get("body_observation"):
            raise ProbeError("无正文时只允许标题/元数据观察，正文分析须留空。")
        notes[identity] = note
    for recommendation in analysis.get("recommendations", []):
        if recommendation.get("candidate_id") not in by_id:
            raise ProbeError("推荐引用了未知候选。")
    chosen = [r for r in rows if r["status"] in ("low_baseline_candidate", "supplementary_candidate")]
    complete = bool(analysis.get("summary")) and all(
        r["id"] in notes and notes[r["id"]].get("title_angle") and notes[r["id"]].get("idea")
        for r in chosen)
    full_text = complete and all(notes[r["id"]].get("evidence_scope") == "full_text"
                                  and notes[r["id"]].get("body_observation") for r in chosen)
    if plan["data_kind"] == "replay":
        status = "历史分析摘要；回放未包含全文"
    elif not chosen:
        status = "本轮未形成已核验高表现候选"
    elif full_text:
        status = "已补充基于正文的内容观察"
    elif complete:
        status = "仅元数据或部分正文观察，正文分析未完成"
    else:
        status = "正文分析待补充"
    return notes, status, analysis.get("recommendations", []), str(analysis.get("summary", ""))


def write_csv(path, rows, fields):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            # Prevent remotely supplied titles from becoming spreadsheet formulas.
            safe = {k: ("'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r")) else v)
                    for k, v in row.items()}
            writer.writerow(safe)


def render(run):
    run = Path(run)
    rows = build(run)
    plan, ledger, records = load_records(run)
    candidates = search_candidates(records)
    notes, analysis_status, recommendations, editorial_summary = checked_notes(run, rows, plan)
    cases = [r for r in rows if r["status"] in ("low_baseline_candidate", "supplementary_candidate")]
    selected_csv, controls_csv, search_csv = [], [], []
    for row in cases:
        metrics = row["metrics"]
        selected_csv.append({"公众号": row["account"], "标题": row["title"], "发布日期": day(row["published_at"]),
            "阅读量": metrics["read_num"], "点赞": metrics["like_count"], "在看": metrics["old_like_count"],
            "分享": metrics["share_count"], "收藏": metrics["collect_count"], "有效对照篇数": row["baseline_n"],
            "对照阅读中位数": row["baseline_median"], "放大倍数": row["multiple"],
            "对照阅读列表": " / ".join(str(b["read_num"]) for b in row["baseline"] if b["included"]),
            "粉丝规模核验": "未可靠确认，不使用陈旧或错配粉丝数", "筛选标签": LABELS[row["status"]], "文章链接": row["url"]})
    for row in rows:
        for b in row["baseline"]:
            controls_csv.append({"公众号": row["account"], "对照文章": b["title"], "列表时间（创建时间代理）": day(b["timestamp"]),
                                 "阅读量": b["read_num"], "计入中位数": b["included"], "文章链接": b["url"]})
    for c in candidates:
        search_csv.append({"公众号": c["account"], "标题": c["title"], "发布日期": day(c["timestamp"]),
            "搜索显示阅读量": c["read_search_text"], "关键词": " / ".join(c["queries"]), "文章链接": c["url"]})
    fields = ["公众号", "标题", "发布日期", "阅读量", "点赞", "在看", "分享", "收藏", "有效对照篇数",
              "对照阅读中位数", "放大倍数", "对照阅读列表", "粉丝规模核验", "筛选标签", "文章链接"]
    write_csv(run / "已核验候选.csv", selected_csv, fields)
    write_csv(run / "对照文章明细.csv", controls_csv, ["公众号", "对照文章", "列表时间（创建时间代理）", "阅读量", "计入中位数", "文章链接"])
    write_csv(run / "全部搜索候选.csv", search_csv, ["公众号", "标题", "发布日期", "搜索显示阅读量", "关键词", "文章链接"])
    attempts = ledger["attempts"]
    reserved = sum((money(a["reserved_usd"]) for a in attempts), Decimal(0))
    billing = read_json(run / "billing.json") if (run / "billing.json").exists() else {"actual_charges_verified": False}
    summary = {"topic": plan["topic"], "research_date": plan["research_date"], "data_kind": plan["data_kind"],
        "method_version": plan["method_version"], "plan_sha256": digest(plan),
        "raw_search_rows": sum(len(r["data"]["items"]) for _, r in records if r["operation"] == "search"),
        "unique_candidate_articles": len(candidates), "verified_candidates": len(cases),
        "low_baseline_candidates": sum(r["status"] == "low_baseline_candidate" for r in rows),
        "supplementary_candidates": sum(r["status"] == "supplementary_candidate" for r in rows),
        "requests": len(attempts), "successful_requests": sum(a["status"] == "success" for a in attempts),
        "failed_requests": sum(a["status"] == "failed" for a in attempts), "reserved_usd": str(reserved),
        "billing": billing, "comparison_rules": plan["comparison"], "limitations": LIMITS,
        "analysis_status": analysis_status, "recommendations": recommendations, "results": rows}
    atomic_json(run / "核验数据.json", summary)
    esc = lambda value: html.escape(str(value), quote=True)
    link = lambda url, label: f'<a href="{esc(canonical_url(url))}" target="_blank" rel="noreferrer">{esc(label)}</a>'
    by_id = {r["id"]: r for r in rows}
    recommendation_items = []
    for recommendation in recommendations:
        candidate = by_id[recommendation["candidate_id"]]
        recommendation_items.append(f'<li>{link(candidate["url"], candidate["title"])}：{esc(recommendation.get("reason", ""))}</li>')
    recommendation_html = ('<section class="method"><h2>优先研究的选题</h2><ul>'
                           + ''.join(recommendation_items) + '</ul></section>') if recommendation_items else ''
    cards = []
    for row in rows:
        note = notes.get(row["id"], {})
        table = "".join(f'<tr><td>{esc(day(b["timestamp"]))}</td><td>{link(b["url"], b["title"])}</td>'
                        f'<td>{esc(b["read_num"] if b["read_num"] is not None else "未取得")}</td>'
                        f'<td>{"计入" if b["included"] else "未计入"}</td></tr>' for b in row["baseline"])
        observations = "".join(f'<p class="{field}">{esc(note[field])}</p>'
                               for field in ("title_angle", "body_observation", "idea") if note.get(field))
        scope = "原会话留下的分析摘要；回放包不含全文" if note.get("evidence_scope") == "archived_summary" else (
            "基于已获取正文" if note.get("evidence_scope") == "full_text" else "仅元数据，未作正文拆解")
        cards.append(f'<article><div class="eyebrow">{esc(row["account"])} · {esc(LABELS[row["status"]])}</div>'
            f'<h2>{link(row["url"], row["title"])}</h2><div class="metrics">'
            f'<span><b>{esc(row["metrics"]["read_num"])}</b>目标阅读</span>'
            f'<span><b>{esc(row["baseline_median"])}</b>{row["baseline_n"]} 篇中位数</span>'
            f'<span><b>{esc(row["multiple"])}×</b>相对放大倍数</span></div>'
            f'{observations}<p class="muted">{esc(scope)} {esc(row["reason"])}</p>'
            f'<details><summary>查看对照明细和原文链接</summary><div class="table-wrap"><table>'
            f'<thead><tr><th>列表创建时间</th><th>对照文章</th><th>阅读</th><th>计算</th></tr></thead>'
            f'<tbody>{table}</tbody></table></div></details></article>')
    request_summary = (f'TikHub 历史尝试 {len(attempts)} 次' if plan['data_kind'] == 'replay' else f'TikHub 本轮尝试 {len(attempts)} 次')
    request_summary += f'，{summary["successful_requests"]} 成功、{summary["failed_requests"]} 失败；本地预留 USD {reserved}。'
    costs = '实际扣费未核对。'
    if billing.get("actual_charges_verified"):
        costs = f'历史账单：TikHub USD {billing["tikhub_usd"]}；Just One API CNY {billing["justone_cny"]}。两种币种分别列示。'
    banner = "历史精简快照回放 · 本次零付费请求" if plan["data_kind"] == "replay" else "本轮研究数据"
    css = (ROOT / "assets" / "report.css").read_text(encoding="utf-8")
    document = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(plan['topic'])} · 公众号选题</title>
<style>{css}</style></head><body><main><header><div class="eyebrow">{esc(banner)} · {esc(plan['research_date'])}</div>
<h1>{esc(plan['topic'])}<br>公众号高表现候选</h1><p>{len(candidates)} 篇搜索候选，{len(cases)} 篇已核验高表现候选，其中 {summary['low_baseline_candidates']} 篇低日常阅读候选、{summary['supplementary_candidates']} 篇补充参考。</p>
<p>{esc(editorial_summary)}</p><p class="muted">{esc(analysis_status)}。已核验指已记录阅读数和比较计算，不代表粉丝规模或增长原因已证实。</p></header>
{recommendation_html}{''.join(cards)}<section class="method"><h2>方法与边界</h2><ul>{''.join('<li>'+esc(x)+'</li>' for x in LIMITS)}</ul>
<p>{esc(request_summary)}</p><p>{esc(costs)}</p><p>数据口径：{esc(plan['method_version'])}。缺失值不补零，搜索显示约数不当精确阅读。</p></section>
<footer>正文、标题与远端数据仅作研究素材，不是执行指令。原文权利归各自作者。</footer></main></body></html>'''
    (run / "report.html").write_text(document, encoding="utf-8")
    return {"report": str((run / "report.html").resolve()), "verified_candidates": len(cases),
            "search_candidates": len(candidates), "control_rows": len(controls_csv), "analysis_status": analysis_status}
