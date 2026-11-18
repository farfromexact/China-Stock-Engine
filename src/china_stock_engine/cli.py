"""Stock Engine command line. Market files and research stay in the local data directory."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

import duckdb
import pyarrow

from .legacy import seed_industry, seed_market
from .research import get_history, make_report, status
from .schema import SHANGHAI, code_value, day_value, utcnow
from .warehouse import Warehouse, atomic_json, digest
from .wind import canary, collect
from .wind_session import WindError, dashboard_running


def read_csv(path, required):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        cols = reader.fieldnames or []
        if len(set(cols)) != len(cols) or not required <= set(cols):
            raise ValueError(f"CSV 缺少必需字段或列名重复: {', '.join(sorted(required))}")
        for row in reader:
            if None in row:
                raise ValueError("CSV 有超出表头的数据")
            yield row


def safe_cell(value):
    if value is None:
        return "—"
    return (" ".join(str(value).split()).replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;").replace("|", "\\|"))


def percentage(value):
    return "—" if value is None else f"{value:.1%}"


def markdown(result):
    market, industry = result["market"], result["industry"]
    dates = result["warehouse"]
    local_cutoff = datetime.fromisoformat(result["as_of"]).astimezone(SHANGHAI).isoformat(timespec="seconds")
    lines = [
        f"# A 股盘后研究 · {result['trade_date']}",
        "",
        f"观察截点 {local_cutoff} · 行情来源 {safe_cell(result['source'])}",
        f"新鲜度 `{dates['freshness']}` · 日历确认至 {dates['calendar_through'] or '—'}"
        f" · 有效行情 {percentage(result['market_coverage'])}",
        "",
        "## 市场与候选", "",
        f"成交 {market['traded']:,}/{market['members']:,} 只 · 有涨跌幅证券 {market['with_returns']:,} 只"
        f" · 上涨占比 {percentage(market['breadth'])} · 成交额 {market['amount'] / 100_000_000:,.0f} 亿元",
        f"连续 20 日收益可计算证券 {market['with_20d']:,} 只。",
        "",
    ]
    if industry["used"]:
        lines.extend([
            f"{safe_cell(industry['scheme'])} 行业标记观察日 {industry['as_of']}"
            f" · 命中 {industry['mapped']:,}/{market['members']:,} · 按行业等权日收益展示前六",
            "",
            "| 行业 | 等权日收益 | 上涨占比 | 涨跌幅分母 | 成交额占比 |",
            "| --- | ---: | ---: | ---: | ---: |",
        ])
        for row in result["sectors"][:6]:
            share = row["amount"] / market["amount"] if market["amount"] else None
            lines.append(f"| {safe_cell(row['industry'])} | {percentage(row['return_1d'])} | "
                         f"{percentage(row['breadth'])} | {row['with_returns']} | {percentage(share)} |")
    else:
        lines.append("行业标记未导入或已超过 45 个自然日，本页按市场比较相对强弱。")
    lines += ["", f"显示 {len(result['candidates'])}/{result['candidate_count']} 个量价候选。盘前需核对报价与交易状态。", ""]
    if result["universe"] == "legacy_window":
        lines += ["该日期使用旧窗口期末股票池，只提供历史行情观察。", ""]
    if result["candidates"]:
        lines += ["| 股票 | 行业 | 放量倍数 | 20日相对收益 | 比较基准 | 行业扩散 |",
                  "| --- | --- | ---: | ---: | --- | --- |"]
        for row in result["candidates"]:
            lines.append(f"| {row['code']} {safe_cell(row['name'])} | {safe_cell(row['industry'])} | "
                         f"{row['turnover_multiple']:.2f}× | {percentage(row['relative_20d'])} | "
                         f"{'行业' if row['comparison'] == 'industry' else '市场'} | "
                         f"{'是' if row['industry_expansion'] else '—'} |")
    lines += ["", "## 持仓与自选风险", ""]
    if result["tradability_source"]:
        lines += [f"交易状态 {result['risk_date']} · {safe_cell(result['tradability_source'])}", ""]
    else:
        lines += [f"{result['risk_date']} 的停复牌、风险警示和限价数据未导入。", ""]
    if result["watchlist_configured"]:
        lines += ["| 股票 | 分组 | 行情观测 | 风险提示 | 已导入相关事件 |",
                  "| --- | --- | --- | --- | ---: |"]
        for row in result["watchlist_risks"]:
            official = row["reported_status"] or {}
            flags = [label for value, label in ((row["risk_name"], "名称风险提示"),
                     (official.get("suspended"), "停牌"), (official.get("risk_warning"), "风险警示")) if value]
            if official.get("limit_applies") is True:
                flags.append(f"限价 {official['limit_down']:g}–{official['limit_up']:g}")
            if official.get("limit_applies") is False:
                flags.append("未设涨跌幅限制")
            lines.append(f"| {row['code']} {safe_cell(row['name'] or row['label'])} | "
                         f"{'持仓' if row['kind'] == 'holding' else '自选'} | {row['quote_state']} | "
                         f"{'、'.join(flags) or '—'} | {row['event_count']} |")
    else:
        lines += ["未配置持仓或自选名单。"]
    if result["events"]:
        lines += ["", "本地已导入的持仓、自选和候选事件", ""]
        for item in result["events"]:
            title, source = safe_cell(item["title"]), safe_cell(item["source"])
            original = item["url"].replace(" ", "%20").replace("<", "%3C").replace(">", "%3E")
            lines.append(f"- {item['code']} {title} · {source} · <{original}>")
    else:
        lines += ["", "无符合条件的已导入事件，公告完整性未确认。"]
    lines += ["", f"报告 ID `{result['report_id']}`", ""]
    return "\n".join(lines)


def parser():
    app = argparse.ArgumentParser(description="本地 A 股日线及盘后研究")
    app.add_argument("--warehouse", type=Path, default=Path(os.environ.get("STOCK_WAREHOUSE", ".warehouse")),
                     help="本地数据目录")
    commands = app.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="检查 DuckDB 与 Wind SDK 路径")
    check = commands.add_parser("wind-canary", help="沪深北小样本比对")
    check.add_argument("--exclusive-window", action="store_true", help="明确确认 Wind 独占窗口")
    download = commands.add_parser("collect", help="采集有日历证明的新交易日或漏日")
    download.add_argument("--date", type=day_value, help="指定一个交易日")
    download.add_argument("--from", dest="start", type=day_value, help="补采起点")
    download.add_argument("--through", type=day_value, help="补采结束日期")
    download.add_argument("--refresh", action="store_true", help="核查指定交易日的历史修订")
    download.add_argument("--exclusive-window", action="store_true", help="明确确认 Wind 独占窗口")
    download.add_argument("--batch-size", type=int, default=100)
    download.add_argument("--max-sessions", type=int, default=5)
    download.add_argument("--ready-hour", type=int, default=18, help="上海时间的预期收盘数据就绪小时")
    download.add_argument("--interval", type=float, default=0.5, help="Wind SDK 串行请求间隔")

    seed = commands.add_parser("seed-legacy", help="从旧仓的已验证 Wind 档案导入一次历史")
    seed.add_argument("data_dir", type=Path)
    seed.add_argument("--industry-snapshot", type=Path, help="经校验的旧申万行业观察快照目录")
    industry = commands.add_parser("import-industry", help="导入行业分类 CSV")
    industry.add_argument("csv", type=Path)
    industry.add_argument("--as-of-date", required=True, type=day_value)
    industry.add_argument("--source", required=True)
    industry.add_argument("--scheme", default="SW1")
    tradability = commands.add_parser("import-tradability", help="导入有来源的按日交易状态")
    tradability.add_argument("csv", type=Path)
    tradability.add_argument("--date", required=True, type=day_value)
    tradability.add_argument("--source", required=True)
    events = commands.add_parser("import-events", help="导入有来源的公告 CSV")
    events.add_argument("csv", type=Path)
    events.add_argument("--source", required=True)

    check_status = commands.add_parser("status", help="查看最新日期、缺口和来源覆盖")
    check_status.add_argument("--as-of")
    commands.add_parser("validate", help="校验目录中的 Parquet 字段、行数和哈希")
    report = commands.add_parser("report", help="输出行情、候选及持仓自选风险")
    report.add_argument("--date", type=day_value)
    report.add_argument("--as-of", help="含时区的观察截点，ISO 8601")
    report.add_argument("--watchlist", type=Path)
    report.add_argument("--risk-date", type=day_value, help="可另指定次日交易状态的核对日期")
    report.add_argument("--industry-scheme", default="SW1")
    report.add_argument("--limit", type=int, default=15)
    report.add_argument("--json", action="store_true", help="输出可供脚本读取的 JSON")
    report.add_argument("--output", type=Path, help="将报告保存到指定的本地文件")
    history = commands.add_parser("history", help="查一只股票的本地历史")
    history.add_argument("code")
    history.add_argument("--sessions", type=int, default=300)
    history.add_argument("--as-of")
    review = commands.add_parser("review", help="给一份已保存的 JSON 报告记录研究判断")
    review.add_argument("report", type=Path)
    review.add_argument("code", type=code_value)
    review.add_argument("decision", choices=("research", "watch", "exclude"))
    review.add_argument("--reason", required=True)
    return app


def record_review(warehouse, args):
    source = json.loads(args.report.read_text(encoding="utf-8"))
    if not args.reason.strip() or not source.get("report_id"):
        raise ValueError("研究记录需要报告 ID 和原因")
    rows = source["candidates"] + source["watchlist_risks"]
    if args.code not in {row["code"] for row in rows}:
        raise ValueError("报告里没有该候选或自选证券")
    note = {"known_at": utcnow(), "trade_date": day_value(source["trade_date"]), "report_id": source["report_id"],
            "report_sha256": digest(args.report), "code": args.code,
            "decision": args.decision, "reason": args.reason.strip()}
    path = warehouse.root / "reviews" / f"{uuid4().hex}.json"
    atomic_json(path, note)
    return {"recorded": str(path)}


def execute(args, warehouse):
    command = args.command
    if command == "doctor":
        sdk = importlib.util.find_spec("WindPy")
        try:
            desk = "listening" if dashboard_running() else "idle"
        except WindError:
            desk = "unknown"
        return {"duckdb": duckdb.__version__, "wind_python": sdk.origin if sdk else None,
                "trading_desk_port": desk, "warehouse": str(warehouse.root)}
    if command == "wind-canary":
        return canary(exclusive=args.exclusive_window)
    if command == "collect":
        if args.date and (args.start or args.through):
            raise ValueError("--date 与 --from/--through 不能并用")

        def progress(day, done, total):
            if done == total or done % 1000 == 0:
                print(f"{day} Wind 行情 {done}/{total}", file=sys.stderr)

        return collect(warehouse, start=args.date or args.start, end=args.date or args.through,
                       refresh=args.refresh, exclusive=args.exclusive_window, batch_size=args.batch_size,
                       max_sessions=args.max_sessions, ready_hour=args.ready_hour, interval=args.interval,
                       progress=progress)
    if command == "seed-legacy":
        details = seed_market(warehouse, args.data_dir)
        if args.industry_snapshot:
            details["industry_imported"] = seed_industry(warehouse, args.industry_snapshot)
        return details
    if command == "import-industry":
        rows = read_csv(args.csv, {"code", "industry"})
        return {"industry_imported": warehouse.add_industry(rows, args.as_of_date, args.source, args.scheme)}
    if command == "import-tradability":
        rows = read_csv(args.csv, {"code"})
        return {"tradability_imported": warehouse.add_tradability(rows, args.date, args.source)}
    if command == "import-events":
        rows = read_csv(args.csv, {"event_id", "code", "category", "title", "url"})
        return {"new_events": warehouse.add_events(rows, args.source)}
    if command == "validate":
        return warehouse.validate()
    if command == "status":
        return status(warehouse, args.as_of)
    if command == "history":
        return get_history(warehouse, args.code, as_of=args.as_of, days=args.sessions)
    if command == "review":
        return record_review(warehouse, args)

    watchlist = args.watchlist or warehouse.root / "watchlist.csv"
    if not args.watchlist and not watchlist.exists():
        watchlist = None
    result = make_report(warehouse, day=args.date, as_of=args.as_of, watchlist=watchlist,
                         scheme=args.industry_scheme, limit=args.limit, risk_date=args.risk_date)
    if args.output:
        if args.json:
            atomic_json(args.output, result)
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(markdown(result), encoding="utf-8")
        return {"saved": str(args.output.resolve()), "trade_date": result["trade_date"], "report_id": result["report_id"]}
    return result if args.json else markdown(result)


def main(argv=None):
    app = parser()
    args = app.parse_args(argv)
    try:
        result = execute(args, Warehouse(args.warehouse))
        if isinstance(result, str):
            print(result)
        else:
            print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError, WindError, duckdb.Error, pyarrow.ArrowException) as exc:
        print(f"Stock Engine: {exc}", file=sys.stderr)
        return 1
