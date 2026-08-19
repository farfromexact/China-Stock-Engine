"""Generate a self-contained local HTML dashboard from the latest snapshot."""

from __future__ import annotations

from datetime import datetime
import html
import json
from pathlib import Path
from typing import Any

import pandas as pd


REQUIRED_FILES = (
    "manifest.json",
    "market_summary.json",
    "universe.parquet",
    "security_reference.parquet",
    "daily_quotes.parquet",
    "trading_calendar.parquet",
    "daily_security_status.parquet",
)


def _safe_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).replace("</", "<\\/")


def _finite_or_none(value: Any, digits: int | None = None) -> Any:
    if value is None or pd.isna(value):
        return None
    number = float(value)
    return round(number, digits) if digits is not None else number


def _money_cn(value: Any) -> str:
    if value is None or pd.isna(value):
        return "—"
    number = float(value)
    if abs(number) >= 1_000_000_000_000:
        return f"¥{number / 1_000_000_000_000:.2f}万亿"
    if abs(number) >= 100_000_000:
        return f"¥{number / 100_000_000:.1f}亿"
    return f"¥{number:,.0f}"


def _percent(value: Any, digits: int = 2) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{float(value):.{digits}f}%"


def _histogram(values: pd.Series) -> list[dict[str, Any]]:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    edges = [-float("inf"), -10, -7, -5, -3, -1, 0, 1, 3, 5, 7, 10, float("inf")]
    labels = [
        "≤-10", "-10~-7", "-7~-5", "-5~-3", "-3~-1", "-1~0",
        "0~1", "1~3", "3~5", "5~7", "7~10", "≥10",
    ]
    bins = pd.cut(numeric, bins=edges, labels=labels, include_lowest=True, right=True)
    counts = bins.value_counts(sort=False)
    return [
        {
            "label": label,
            "count": int(counts.get(label, 0)),
            "direction": "down" if index < 6 else "up",
        }
        for index, label in enumerate(labels)
    ]


def _artifact_schema_html(frames: dict[str, pd.DataFrame]) -> str:
    labels = {
        "universe.parquet": "证券池",
        "security_reference.parquet": "PIT 证券主数据",
        "daily_quotes.parquet": "扩展日行情",
        "trading_calendar.parquet": "交易日历",
        "daily_security_status.parquet": "逐证券观测状态",
    }
    blocks: list[str] = []
    for name, frame in frames.items():
        fields = "".join(f"<code>{html.escape(str(column))}</code>" for column in frame.columns)
        blocks.append(
            "<details class=\"schema-item\">"
            f"<summary><span>{labels[name]}</span><strong>{len(frame):,} 行</strong></summary>"
            f"<div class=\"schema-fields\">{fields}</div>"
            "</details>"
        )
    return "".join(blocks)


def _distribution_html(histogram: list[dict[str, Any]]) -> str:
    maximum = max((item["count"] for item in histogram), default=1)
    bars: list[str] = []
    for item in histogram:
        height = 8 + 92 * item["count"] / maximum
        bars.append(
            "<div class=\"hist-column\">"
            f"<span class=\"hist-count\">{item['count']:,}</span>"
            f"<div class=\"hist-bar {item['direction']}\" style=\"height:{height:.2f}%\"></div>"
            f"<span class=\"hist-label\">{html.escape(item['label'])}</span>"
            "</div>"
        )
    return "".join(bars)


def _bar_list_html(values: dict[str, int], total: int) -> str:
    rows: list[str] = []
    for label, count in sorted(values.items(), key=lambda item: item[1], reverse=True):
        width = 100 * count / total if total else 0
        rows.append(
            "<div class=\"bar-row\">"
            f"<div class=\"bar-meta\"><span>{html.escape(label)}</span>"
            f"<strong>{count:,}</strong></div>"
            "<div class=\"bar-track\"><span class=\"bar-fill\" "
            f"style=\"width:{width:.3f}%\"></span></div>"
            "</div>"
        )
    return "".join(rows)


def build_dashboard(data_dir: Path, output_path: Path) -> Path:
    """Build an offline HTML page from ``data/latest`` normalized artifacts."""

    latest = data_dir / "latest"
    missing = [name for name in REQUIRED_FILES if not (latest / name).exists()]
    if missing:
        raise FileNotFoundError("latest snapshot missing: " + ", ".join(missing))

    manifest = json.loads((latest / "manifest.json").read_text(encoding="utf-8"))
    summary = json.loads((latest / "market_summary.json").read_text(encoding="utf-8"))
    if manifest.get("verified") is not True or manifest.get("data_fresh") is not True:
        raise ValueError("latest snapshot is not verified and fresh")

    frames = {
        "universe.parquet": pd.read_parquet(latest / "universe.parquet"),
        "security_reference.parquet": pd.read_parquet(
            latest / "security_reference.parquet"
        ),
        "daily_quotes.parquet": pd.read_parquet(latest / "daily_quotes.parquet"),
        "trading_calendar.parquet": pd.read_parquet(
            latest / "trading_calendar.parquet"
        ),
        "daily_security_status.parquet": pd.read_parquet(
            latest / "daily_security_status.parquet"
        ),
    }
    reference = frames["security_reference.parquet"]
    quotes = frames["daily_quotes.parquet"]
    status = frames["daily_security_status.parquet"]

    explorer = status.loc[
        :,
        [
            "thscode",
            "security_name",
            "exchange",
            "board",
            "observation_state",
        ],
    ].merge(
        reference.loc[
            :, ["thscode", "listing_date", "total_shares", "float_a_shares"]
        ],
        how="left",
        on="thscode",
        validate="one_to_one",
    )
    explorer = explorer.merge(
        quotes.loc[
            :,
            [
                "thscode",
                "close",
                "pre_close",
                "avg_price",
                "volume",
                "amount",
                "turnover_ratio",
                "change_ratio",
            ],
        ],
        how="left",
        on="thscode",
        validate="one_to_one",
    ).sort_values("thscode")

    rows: list[list[Any]] = []
    for row in explorer.itertuples(index=False):
        rows.append(
            [
                str(row.thscode),
                str(row.security_name),
                str(row.exchange),
                str(row.board),
                str(row.observation_state),
                str(row.listing_date) if not pd.isna(row.listing_date) else None,
                _finite_or_none(row.total_shares, 0),
                _finite_or_none(row.float_a_shares, 0),
                _finite_or_none(row.close, 4),
                _finite_or_none(row.pre_close, 4),
                _finite_or_none(row.avg_price, 6),
                _finite_or_none(row.volume, 0),
                _finite_or_none(row.amount, 2),
                _finite_or_none(row.turnover_ratio, 4),
                _finite_or_none(row.change_ratio, 4),
            ]
        )

    boards = {
        str(key): int(value)
        for key, value in quotes.groupby("board", dropna=False).size().items()
    }
    exchanges = {
        str(key): int(value)
        for key, value in quotes.groupby("exchange", dropna=False).size().items()
    }
    histogram = _histogram(quotes["change_ratio"])
    quality = (manifest.get("quality") or {}).get("metrics") or {}
    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    trade_date = str(manifest.get("trade_date") or "")
    advancers = int(summary.get("advancers") or 0)
    decliners = int(summary.get("decliners") or 0)
    unchanged = int(summary.get("unchanged") or 0)
    quoted = int(summary.get("quoted_securities") or 0)
    breadth_total = max(advancers + decliners + unchanged, 1)

    meta = {
        "tradeDate": trade_date,
        "generatedAt": generated_at,
        "universeCount": int(quality.get("universe_count") or len(status)),
        "quoteCount": int(quality.get("quote_count") or len(quotes)),
        "quoteCoverage": float(quality.get("quote_coverage") or 0),
        "referenceCoverage": float(quality.get("reference_coverage") or 0),
        "qualityErrors": list((manifest.get("quality") or {}).get("errors") or []),
    }
    board_options = "".join(
        f"<option value=\"{html.escape(value)}\">{html.escape(value)}</option>"
        for value in sorted(explorer["board"].dropna().astype(str).unique())
    )

    template = r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:">
  <title>China Stock Engine · __TRADE_DATE__</title>
  <style>
    :root{color-scheme:dark;--bg:#080d18;--surface:#101827;--surface-2:#151f31;--line:#243047;--text:#e8edf6;--muted:#8e9bb0;--red:#f05b68;--green:#3bc08d;--blue:#5b8ff9;--amber:#f4bd4f;--cyan:#42c5d7;--shadow:0 14px 36px rgba(0,0,0,.28)}
    *{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 12% -10%,#172647 0,transparent 34%),var(--bg);color:var(--text);font-family:Inter,"Segoe UI","Microsoft YaHei",sans-serif;font-size:14px;line-height:1.5}button,input,select{font:inherit}.shell{max-width:1440px;margin:0 auto;padding:28px 24px 48px}.topbar{display:flex;justify-content:space-between;gap:24px;align-items:flex-start;margin-bottom:22px}.brand{display:flex;gap:14px;align-items:center}.logo{width:42px;height:42px;border-radius:12px;background:linear-gradient(135deg,var(--blue),var(--cyan));display:grid;place-items:center;font-weight:700;color:#06101c;box-shadow:0 8px 24px rgba(91,143,249,.32)}h1{font-size:24px;line-height:1.2;margin:0 0 5px;font-weight:600;letter-spacing:.01em}.subtitle{color:var(--muted);font-size:13px}.status{display:flex;align-items:center;gap:8px;background:rgba(59,192,141,.1);color:#73ddb2;border:1px solid rgba(59,192,141,.26);padding:7px 11px;border-radius:999px;white-space:nowrap}.status-dot{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 0 4px rgba(59,192,141,.12)}.kpis{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:12px;margin-bottom:16px}.card,.panel{background:linear-gradient(180deg,rgba(21,31,49,.96),rgba(16,24,39,.96));border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow)}.card{padding:16px 17px;min-height:104px}.k-label{color:var(--muted);font-size:12px;margin-bottom:8px}.k-value{font-size:26px;font-weight:600;line-height:1.1;font-variant-numeric:tabular-nums}.k-note{margin-top:8px;color:var(--muted);font-size:12px}.negative{color:var(--green)}.positive{color:var(--red)}.quality-line{display:flex;gap:18px;align-items:center;padding:11px 14px;margin-bottom:16px;background:rgba(59,192,141,.07);border:1px solid rgba(59,192,141,.18);border-radius:12px;color:#bcefd9}.quality-line strong{color:#7ee0b8}.chart-grid{display:grid;grid-template-columns:1.3fr 1fr 1fr;gap:14px;margin-bottom:16px}.panel{padding:18px}.panel h2{font-size:15px;margin:0 0 4px;font-weight:600}.panel-sub{color:var(--muted);font-size:12px;margin-bottom:18px}.breadth{display:flex;height:20px;border-radius:7px;overflow:hidden;background:#202a3d;margin:18px 0 12px}.breadth span{display:block;height:100%}.breadth .up{background:var(--red)}.breadth .flat{background:#718096}.breadth .down{background:var(--green)}.legend{display:flex;gap:16px;flex-wrap:wrap;color:var(--muted);font-size:12px}.legend b{color:var(--text);font-weight:600}.swatch{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:6px}.histogram{height:190px;display:flex;gap:6px;align-items:flex-end;padding-top:22px;border-bottom:1px solid var(--line)}.hist-column{height:100%;flex:1;min-width:0;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;position:relative}.hist-count{font-size:10px;color:var(--muted);margin-bottom:4px;font-variant-numeric:tabular-nums}.hist-bar{width:72%;min-height:3px;border-radius:4px 4px 0 0}.hist-bar.up{background:linear-gradient(180deg,#ff7a84,var(--red))}.hist-bar.down{background:linear-gradient(180deg,#60d5a9,var(--green))}.hist-label{font-size:9px;color:var(--muted);white-space:nowrap;transform:rotate(-42deg);transform-origin:center;margin-top:18px;height:28px}.bar-list{display:grid;gap:11px}.bar-meta{display:flex;justify-content:space-between;font-size:12px}.bar-meta strong{font-weight:600;font-variant-numeric:tabular-nums}.bar-track{height:6px;background:#202a3d;border-radius:5px;overflow:hidden;margin-top:5px}.bar-fill{display:block;height:100%;background:linear-gradient(90deg,var(--blue),var(--cyan));border-radius:5px}.exchange{display:flex;height:12px;overflow:hidden;border-radius:5px;margin:17px 0 16px}.exchange span:nth-child(1){background:var(--blue)}.exchange span:nth-child(2){background:var(--cyan)}.exchange span:nth-child(3){background:var(--amber)}.exchange-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.exchange-item{background:rgba(255,255,255,.025);padding:10px;border-radius:9px}.exchange-item span{display:block;color:var(--muted);font-size:11px}.exchange-item strong{font-size:17px;font-weight:600;font-variant-numeric:tabular-nums}.explorer{margin-top:16px}.explorer-head{display:flex;justify-content:space-between;gap:20px;align-items:flex-end;margin-bottom:14px}.explorer-head h2{font-size:18px;margin:0 0 4px}.controls{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end}.control{background:#0c1423;color:var(--text);border:1px solid var(--line);border-radius:8px;padding:8px 10px;min-height:38px}.search{min-width:220px}.control:focus{outline:2px solid rgba(91,143,249,.65);outline-offset:1px}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:10px}.data-table{width:100%;border-collapse:collapse;min-width:1180px;font-size:12px}.data-table th{position:sticky;top:0;background:#172135;color:#a9b5c8;text-align:left;font-weight:500;padding:10px 11px;border-bottom:1px solid var(--line);white-space:nowrap}.data-table td{padding:9px 11px;border-bottom:1px solid rgba(36,48,71,.64);white-space:nowrap;font-variant-numeric:tabular-nums}.data-table tr:hover td{background:rgba(91,143,249,.055)}.data-table td.num,.data-table th.num{text-align:right}.state-label{display:inline-flex;padding:2px 7px;border-radius:999px;background:rgba(59,192,141,.1);color:#82dfba}.state-label.missing{background:rgba(244,189,79,.1);color:#f3ca74}.pager{display:flex;justify-content:space-between;align-items:center;margin-top:12px;color:var(--muted);font-size:12px}.pager-buttons{display:flex;gap:7px}.pager button{background:#121d2e;color:var(--text);border:1px solid var(--line);border-radius:7px;padding:7px 11px;cursor:pointer}.pager button:disabled{opacity:.38;cursor:default}.schemas{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}.schema-item{border:1px solid var(--line);border-radius:9px;background:rgba(255,255,255,.018)}.schema-item summary{display:flex;justify-content:space-between;gap:12px;padding:11px 13px;cursor:pointer}.schema-item summary strong{font-size:12px;color:var(--muted)}.schema-fields{display:flex;gap:6px;flex-wrap:wrap;padding:0 13px 13px}.schema-fields code{background:#0b1321;color:#9ab6e9;border:1px solid #24324a;border-radius:5px;padding:3px 5px;font-size:10px}.foot{display:flex;justify-content:space-between;gap:20px;color:var(--muted);font-size:11px;margin-top:18px;padding:0 2px}.empty{text-align:center;color:var(--muted);padding:34px!important}
    @media(max-width:1080px){.kpis{grid-template-columns:repeat(3,1fr)}.chart-grid{grid-template-columns:1fr 1fr}.chart-grid .distribution{grid-column:1/-1}.explorer-head{align-items:flex-start;flex-direction:column}.controls{justify-content:flex-start}}
    @media(max-width:700px){.shell{padding:18px 12px 34px}.topbar{flex-direction:column}.kpis{grid-template-columns:1fr 1fr}.chart-grid{grid-template-columns:1fr}.chart-grid .distribution{grid-column:auto}.schemas{grid-template-columns:1fr}.controls{width:100%}.control{flex:1;min-width:130px}.search{min-width:100%}.quality-line{align-items:flex-start;flex-direction:column;gap:4px}.foot{flex-direction:column}.k-value{font-size:22px}}
  </style>
</head>
<body>
<main class="shell">
  <header class="topbar">
    <div class="brand"><div class="logo">CS</div><div><h1>China Stock Engine</h1><div class="subtitle">A 股日频数据快照 · __TRADE_DATE__ · iFinD HTTP</div></div></div>
    <div class="status"><span class="status-dot"></span>Verified · Schema v2</div>
  </header>

  <section class="kpis" aria-label="市场概览">
    <article class="card"><div class="k-label">证券池</div><div class="k-value">__UNIVERSE_COUNT__</div><div class="k-note">SSE · SZSE · BSE</div></article>
    <article class="card"><div class="k-label">有效行情</div><div class="k-value">__QUOTE_COUNT__</div><div class="k-note">覆盖率 __QUOTE_COVERAGE__</div></article>
    <article class="card"><div class="k-label">全市场成交额</div><div class="k-value">__TOTAL_AMOUNT__</div><div class="k-note">归一化 iFinD amount</div></article>
    <article class="card"><div class="k-label">等权涨跌幅</div><div class="k-value __RETURN_CLASS__">__EQUAL_RETURN__</div><div class="k-note">中位数 __MEDIAN_RETURN__</div></article>
    <article class="card"><div class="k-label">上涨 / 下跌</div><div class="k-value"><span class="positive">__ADVANCERS__</span> / <span class="negative">__DECLINERS__</span></div><div class="k-note">平盘 __UNCHANGED__ 家</div></article>
  </section>

  <div class="quality-line"><strong>质量门禁通过</strong><span>证券主数据覆盖 __REFERENCE_COVERAGE__ · 扩展行情字段覆盖 100% · OHLC / 股本 / 涨跌幅一致性错误均为 0</span></div>

  <section class="chart-grid">
    <article class="panel"><h2>市场宽度</h2><div class="panel-sub">上涨、平盘与下跌家数</div><div class="breadth" role="img" aria-label="上涨 __ADVANCERS__ 家，平盘 __UNCHANGED__ 家，下跌 __DECLINERS__ 家"><span class="up" style="width:__ADV_WIDTH__%"></span><span class="flat" style="width:__FLAT_WIDTH__%"></span><span class="down" style="width:__DOWN_WIDTH__%"></span></div><div class="legend"><span><i class="swatch" style="background:var(--red)"></i>上涨 <b>__ADVANCERS__</b></span><span><i class="swatch" style="background:#718096"></i>平盘 <b>__UNCHANGED__</b></span><span><i class="swatch" style="background:var(--green)"></i>下跌 <b>__DECLINERS__</b></span></div><div class="exchange" aria-label="交易所行情数量"><span style="width:__SSE_WIDTH__%"></span><span style="width:__SZSE_WIDTH__%"></span><span style="width:__BSE_WIDTH__%"></span></div><div class="exchange-grid"><div class="exchange-item"><span>SSE</span><strong>__SSE_COUNT__</strong></div><div class="exchange-item"><span>SZSE</span><strong>__SZSE_COUNT__</strong></div><div class="exchange-item"><span>BSE</span><strong>__BSE_COUNT__</strong></div></div></article>
    <article class="panel"><h2>板块覆盖</h2><div class="panel-sub">有日行情的证券数量</div><div class="bar-list">__BOARD_BARS__</div></article>
    <article class="panel distribution"><h2>涨跌幅分布</h2><div class="panel-sub">区间单位：%</div><div class="histogram" role="img" aria-label="全市场涨跌幅分布">__HISTOGRAM__</div></article>
  </section>

  <section class="panel explorer">
    <div class="explorer-head"><div><h2>逐证券数据浏览</h2><div class="panel-sub" id="resultSummary">载入中</div></div><div class="controls"><input class="control search" id="searchInput" type="search" placeholder="搜索代码或证券名称" aria-label="搜索代码或证券名称"><select class="control" id="exchangeFilter" aria-label="交易所"><option value="">全部交易所</option><option>SSE</option><option>SZSE</option><option>BSE</option></select><select class="control" id="boardFilter" aria-label="板块"><option value="">全部板块</option>__BOARD_OPTIONS__</select><select class="control" id="stateFilter" aria-label="观测状态"><option value="">全部状态</option><option value="traded">有成交</option><option value="no_quote_observed">无行情观测</option><option value="quote_without_turnover">有行情无成交</option></select><select class="control" id="sortSelect" aria-label="排序"><option value="code_asc">代码升序</option><option value="change_desc">涨幅最高</option><option value="change_asc">跌幅最大</option><option value="amount_desc">成交额最大</option><option value="turnover_desc">换手率最高</option></select></div></div>
    <div class="table-wrap"><table class="data-table"><thead><tr><th>代码</th><th>名称</th><th>市场</th><th>板块</th><th>状态</th><th class="num">收盘</th><th class="num">涨跌幅</th><th class="num">均价</th><th class="num">换手率</th><th class="num">成交额</th><th>上市日期</th><th class="num">总股本</th><th class="num">流通 A 股</th></tr></thead><tbody id="tableBody"></tbody></table></div>
    <div class="pager"><span id="pageInfo"></span><div class="pager-buttons"><button id="prevPage" type="button">上一页</button><button id="nextPage" type="button">下一页</button></div></div>
  </section>

  <section class="panel explorer"><h2>本次快照的数据表</h2><div class="panel-sub">展开查看实际字段；页面只嵌入标准化结果，不包含 token 或原始响应。</div><div class="schemas">__SCHEMA_HTML__</div></section>
  <footer class="foot"><span>本地研究用途 · iFinD 商业数据请勿公开再分发</span><span>页面生成：__GENERATED_AT__</span></footer>
</main>
<script>
const rows=__ROWS_JSON__;
const meta=__META_JSON__;
const PAGE_SIZE=50;
let page=1;
let filtered=[];
const searchInput=document.getElementById('searchInput');
const exchangeFilter=document.getElementById('exchangeFilter');
const boardFilter=document.getElementById('boardFilter');
const stateFilter=document.getElementById('stateFilter');
const sortSelect=document.getElementById('sortSelect');
const tableBody=document.getElementById('tableBody');
const pageInfo=document.getElementById('pageInfo');
const resultSummary=document.getElementById('resultSummary');
const prevPage=document.getElementById('prevPage');
const nextPage=document.getElementById('nextPage');
function esc(value){return String(value??'').replace(/[&<>"']/g,function(char){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]});}
function number(value,digits){if(value===null||value===undefined)return '—';return Number(value).toLocaleString('zh-CN',{minimumFractionDigits:digits,maximumFractionDigits:digits});}
function compact(value){if(value===null||value===undefined)return '—';const n=Number(value);if(Math.abs(n)>=1e12)return (n/1e12).toFixed(2)+'万亿';if(Math.abs(n)>=1e8)return (n/1e8).toFixed(2)+'亿';if(Math.abs(n)>=1e4)return (n/1e4).toFixed(1)+'万';return n.toLocaleString('zh-CN',{maximumFractionDigits:0});}
function stateLabel(value){if(value==='traded')return '<span class="state-label">有成交</span>';if(value==='no_quote_observed')return '<span class="state-label missing">无行情观测</span>';return '<span class="state-label missing">有行情无成交</span>';}
function compareNullable(a,b,direction){const av=a===null?(direction>0?Infinity:-Infinity):a;const bv=b===null?(direction>0?Infinity:-Infinity):b;return (av-bv)*direction;}
function applyFilters(){const term=searchInput.value.trim().toLowerCase();filtered=rows.filter(function(row){return(!term||row[0].toLowerCase().includes(term)||row[1].toLowerCase().includes(term))&&(!exchangeFilter.value||row[2]===exchangeFilter.value)&&(!boardFilter.value||row[3]===boardFilter.value)&&(!stateFilter.value||row[4]===stateFilter.value);});const sort=sortSelect.value;if(sort==='code_asc')filtered.sort(function(a,b){return a[0].localeCompare(b[0]);});if(sort==='change_desc')filtered.sort(function(a,b){return compareNullable(a[14],b[14],-1);});if(sort==='change_asc')filtered.sort(function(a,b){return compareNullable(a[14],b[14],1);});if(sort==='amount_desc')filtered.sort(function(a,b){return compareNullable(a[12],b[12],-1);});if(sort==='turnover_desc')filtered.sort(function(a,b){return compareNullable(a[13],b[13],-1);});page=1;render();}
function render(){const pages=Math.max(1,Math.ceil(filtered.length/PAGE_SIZE));page=Math.min(Math.max(page,1),pages);const start=(page-1)*PAGE_SIZE;const view=filtered.slice(start,start+PAGE_SIZE);if(!view.length){tableBody.innerHTML='<tr><td class="empty" colspan="13">没有符合条件的证券</td></tr>';}else{tableBody.innerHTML=view.map(function(r){const changeClass=r[14]===null?'':(r[14]>0?'positive':(r[14]<0?'negative':''));return '<tr><td>'+esc(r[0])+'</td><td>'+esc(r[1])+'</td><td>'+esc(r[2])+'</td><td>'+esc(r[3])+'</td><td>'+stateLabel(r[4])+'</td><td class="num">'+number(r[8],2)+'</td><td class="num '+changeClass+'">'+(r[14]===null?'—':number(r[14],2)+'%')+'</td><td class="num">'+number(r[10],4)+'</td><td class="num">'+(r[13]===null?'—':number(r[13],2)+'%')+'</td><td class="num">'+compact(r[12])+'</td><td>'+esc(r[5]||'—')+'</td><td class="num">'+compact(r[6])+'</td><td class="num">'+compact(r[7])+'</td></tr>';}).join('');}resultSummary.textContent='筛选结果 '+filtered.length.toLocaleString('zh-CN')+' / '+meta.universeCount.toLocaleString('zh-CN')+' 只证券';pageInfo.textContent='第 '+page+' / '+pages+' 页 · 每页 '+PAGE_SIZE+' 行';prevPage.disabled=page<=1;nextPage.disabled=page>=pages;}
[searchInput,exchangeFilter,boardFilter,stateFilter,sortSelect].forEach(function(element){element.addEventListener(element===searchInput?'input':'change',applyFilters);});
prevPage.addEventListener('click',function(){page-=1;render();});nextPage.addEventListener('click',function(){page+=1;render();});
applyFilters();
</script>
</body>
</html>'''

    replacements = {
        "__TRADE_DATE__": html.escape(trade_date),
        "__UNIVERSE_COUNT__": f"{meta['universeCount']:,}",
        "__QUOTE_COUNT__": f"{meta['quoteCount']:,}",
        "__QUOTE_COVERAGE__": f"{meta['quoteCoverage']:.2%}",
        "__TOTAL_AMOUNT__": _money_cn(summary.get("total_amount")),
        "__EQUAL_RETURN__": _percent(summary.get("equal_weight_change_pct")),
        "__MEDIAN_RETURN__": _percent(summary.get("median_change_pct")),
        "__RETURN_CLASS__": "positive"
        if float(summary.get("equal_weight_change_pct") or 0) > 0
        else "negative",
        "__ADVANCERS__": f"{advancers:,}",
        "__DECLINERS__": f"{decliners:,}",
        "__UNCHANGED__": f"{unchanged:,}",
        "__REFERENCE_COVERAGE__": f"{meta['referenceCoverage']:.2%}",
        "__ADV_WIDTH__": f"{100 * advancers / breadth_total:.4f}",
        "__FLAT_WIDTH__": f"{100 * unchanged / breadth_total:.4f}",
        "__DOWN_WIDTH__": f"{100 * decliners / breadth_total:.4f}",
        "__SSE_WIDTH__": f"{100 * exchanges.get('SSE', 0) / max(quoted, 1):.4f}",
        "__SZSE_WIDTH__": f"{100 * exchanges.get('SZSE', 0) / max(quoted, 1):.4f}",
        "__BSE_WIDTH__": f"{100 * exchanges.get('BSE', 0) / max(quoted, 1):.4f}",
        "__SSE_COUNT__": f"{exchanges.get('SSE', 0):,}",
        "__SZSE_COUNT__": f"{exchanges.get('SZSE', 0):,}",
        "__BSE_COUNT__": f"{exchanges.get('BSE', 0):,}",
        "__BOARD_BARS__": _bar_list_html(boards, quoted),
        "__HISTOGRAM__": _distribution_html(histogram),
        "__BOARD_OPTIONS__": board_options,
        "__SCHEMA_HTML__": _artifact_schema_html(frames),
        "__GENERATED_AT__": html.escape(generated_at),
        "__ROWS_JSON__": _safe_json(rows),
        "__META_JSON__": _safe_json(meta),
    }
    document = template
    for marker, value in replacements.items():
        document = document.replace(marker, value)
    if "__" in document:
        unresolved = sorted(
            {part.split("__", 1)[0] for part in document.split("__")[1::2]}
        )
        raise RuntimeError(f"dashboard template has unresolved markers: {unresolved}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.tmp")
    temporary.write_text(document, encoding="utf-8", newline="\n")
    temporary.replace(output_path)
    return output_path


__all__ = ["build_dashboard"]
