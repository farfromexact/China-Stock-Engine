"""Offline HTML data-reference page built from the compact JSON contract."""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any


def _load_json(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return loaded


def _text(value: Any, fallback: str = "—") -> str:
    if value is None or value == "":
        return fallback
    return html.escape(str(value))


def _integer(value: Any) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "—"


def _amount(value: Any) -> str:
    try:
        return f"{float(value) / 100_000_000:,.1f} 亿元"
    except (TypeError, ValueError):
        return "—"


def _percent(value: Any, *, ratio: bool = False) -> str:
    try:
        number = float(value) * (100 if ratio else 1)
        return f"{number:,.2f}%"
    except (TypeError, ValueError):
        return "—"


def _status_class(state: Any) -> str:
    normalized = str(state or "missing").lower()
    return normalized if normalized in {"ready", "stale", "missing", "not_entitled"} else "missing"


def _readiness_cards(readiness: dict[str, Any]) -> str:
    cards: list[str] = []
    for name, payload in sorted(readiness.items()):
        item = payload if isinstance(payload, dict) else {}
        state = str(item.get("state") or "missing")
        details = [
            f"{key}: {value}"
            for key, value in sorted(item.items())
            if key != "state" and not isinstance(value, (dict, list))
        ]
        cards.append(
            '<article class="module">'
            f"<div><strong>{_text(name)}</strong>"
            f'<span class="badge {_status_class(state)}">{_text(state)}</span></div>'
            f"<p>{_text(' · '.join(details[:4]))}</p>"
            "</article>"
        )
    return "".join(cards) or '<p class="empty">暂无模块状态。</p>'


def _catalog_rows(items: list[dict[str, Any]]) -> str:
    rows: list[str] = []
    for item in items:
        digest = str(item.get("sha256") or "")
        rows.append(
            "<tr>"
            f"<td><strong>{_text(item.get('name'))}</strong><small>{_text(item.get('description'))}</small></td>"
            f"<td>{_text(item.get('layer'))}</td>"
            f"<td><code>{_text(item.get('path'))}</code></td>"
            f"<td class=\"num\">{_integer(item.get('rows'))}</td>"
            f"<td><code>{_text(digest[:12])}</code></td>"
            "</tr>"
        )
    return "".join(rows)


def _coverage_rows(items: list[dict[str, Any]]) -> str:
    rows: list[str] = []
    for item in items:
        ratio = item.get("coverage_ratio")
        try:
            width = max(0.0, min(100.0, float(ratio) * 100))
        except (TypeError, ValueError):
            width = 0.0
        rows.append(
            "<tr>"
            f"<td><code>{_text(item.get('field'))}</code></td>"
            f"<td class=\"num\">{_integer(item.get('non_null_count'))}</td>"
            f"<td class=\"num\">{_percent(ratio, ratio=True)}</td>"
            f'<td><div class="bar"><i style="width:{width:.2f}%"></i></div></td>'
            "</tr>"
        )
    return "".join(rows)


def _industry_rows(items: list[dict[str, Any]]) -> str:
    rows: list[str] = []
    for item in items:
        rows.append(
            "<tr>"
            f"<td>{_text(item.get('sw1_code'))}</td>"
            f"<td><strong>{_text(item.get('sw1_name'))}</strong></td>"
            f"<td class=\"num\">{_integer(item.get('member_count'))}</td>"
            f"<td class=\"num\">{_percent(item.get('mean_return_20d_pct'))}</td>"
            f"<td class=\"num\">{_percent(item.get('median_return_20d_pct'))}</td>"
            f"<td class=\"num\">{_percent(item.get('positive_breadth_20d'), ratio=True)}</td>"
            "</tr>"
        )
    return "".join(rows)


def build_data_reference_dashboard(data_dir: Path, output: Path) -> Path:
    """Render a self-contained factual data reference with no scripts or opinions."""

    reference_path = data_dir / "latest" / "data_reference_latest.json"
    if not reference_path.exists():
        raise FileNotFoundError("latest data_reference_latest.json does not exist")
    reference = _load_json(reference_path)
    manifest = _load_json(data_dir / "latest" / "manifest.json")
    source_hash = str((reference.get("run") or {}).get("source_snapshot_sha256") or "")
    manifest_hash = str(
        (manifest.get("data_reference") or {}).get("source_snapshot_sha256") or ""
    )
    if not source_hash or source_hash != manifest_hash:
        raise ValueError("data reference and manifest source hashes do not match")

    as_of = reference.get("as_of") or {}
    market = reference.get("market_snapshot") or {}
    breadth = market.get("breadth") or {}
    coverage = reference.get("coverage") or {}
    catalog = list(reference.get("data_catalog") or [])
    readiness = reference.get("readiness") or {}
    industries = list(market.get("industry_summary") or [])
    quote_coverage = (
        ((reference.get("quality") or {}).get("quality_gate") or {})
        .get("metrics", {})
        .get("quote_coverage")
    )

    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:">
<title>China Stock Data Reference</title>
<style>
:root{{--bg:#0b1020;--panel:#121a2d;--panel2:#172139;--line:#263451;--text:#edf2ff;--muted:#93a4c7;--blue:#70a5ff;--green:#55d6a0;--amber:#f4c86a;--red:#ff7d8b}}
*{{box-sizing:border-box}} body{{margin:0;background:linear-gradient(145deg,#090e1b,#0d1425 55%,#0a1020);color:var(--text);font:14px/1.55 Inter,"Segoe UI","Microsoft YaHei",sans-serif}}
main{{width:min(1480px,94vw);margin:32px auto 56px}} header{{display:flex;justify-content:space-between;gap:24px;align-items:end;margin-bottom:22px}} h1{{margin:0;font-size:28px;letter-spacing:.2px}} h1 span{{display:block;color:var(--blue);font-size:12px;letter-spacing:2px;text-transform:uppercase;margin-bottom:5px}} h2{{font-size:17px;margin:0 0 16px}} p{{margin:0;color:var(--muted)}} .hash{{max-width:420px;text-align:right;word-break:break-all}} .metrics{{display:grid;grid-template-columns:repeat(6,minmax(130px,1fr));gap:12px;margin-bottom:14px}} .metric,.panel,.module{{border:1px solid var(--line);background:rgba(18,26,45,.94);border-radius:14px}} .metric{{padding:16px}} .metric span{{display:block;color:var(--muted);font-size:12px}} .metric strong{{display:block;font-size:21px;margin-top:5px}} .panel{{padding:20px;margin-top:14px;overflow:hidden}} .modules{{display:grid;grid-template-columns:repeat(4,minmax(210px,1fr));gap:10px}} .module{{padding:13px;background:var(--panel2)}} .module div{{display:flex;align-items:center;justify-content:space-between;gap:10px}} .module p{{font-size:12px;margin-top:8px;min-height:18px}} .badge{{font-size:11px;border-radius:999px;padding:3px 8px;border:1px solid currentColor}} .badge.ready{{color:var(--green)}} .badge.stale,.badge.not_entitled{{color:var(--amber)}} .badge.missing{{color:var(--red)}} .scroll{{overflow:auto;max-height:520px}} table{{border-collapse:collapse;width:100%;min-width:760px}} th,td{{padding:10px 11px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}} th{{position:sticky;top:0;background:var(--panel);color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.5px}} td small{{display:block;color:var(--muted);margin-top:2px}} td.num{{text-align:right;font-variant-numeric:tabular-nums}} code{{font:12px/1.4 "Cascadia Code",Consolas,monospace;color:#bcd2ff}} .bar{{height:7px;background:#202c47;border-radius:99px;min-width:120px;overflow:hidden}} .bar i{{display:block;height:100%;background:linear-gradient(90deg,#4f8fff,#55d6a0)}} .two{{display:grid;grid-template-columns:1.15fr .85fr;gap:14px}} footer{{color:var(--muted);font-size:12px;padding:20px 3px}} .empty{{padding:18px}}
@media(max-width:1000px){{.metrics{{grid-template-columns:repeat(3,1fr)}}.modules{{grid-template-columns:repeat(2,1fr)}}.two{{grid-template-columns:1fr}}}}
@media(max-width:620px){{main{{width:94vw;margin-top:20px}}header{{display:block}}.hash{{text-align:left;margin-top:10px}}.metrics{{grid-template-columns:repeat(2,1fr)}}.modules{{grid-template-columns:1fr}}}}
</style>
</head>
<body><main>
<header><div><h1><span>A-share verified data</span>中国股票数据参考</h1><p>事实层、PIT 派生字段、覆盖率与可追溯路径</p></div><p class="hash">源快照 <code>{_text(source_hash)}</code></p></header>
<section class="metrics">
  <article class="metric"><span>源交易日</span><strong>{_text(as_of.get('source_trade_date'))}</strong></article>
  <article class="metric"><span>逐股状态行数</span><strong>{_integer(coverage.get('stock_state_rows'))}</strong></article>
  <article class="metric"><span>有行情证券</span><strong>{_integer(market.get('quoted_securities'))}</strong></article>
  <article class="metric"><span>行情覆盖率</span><strong>{_percent(quote_coverage, ratio=True)}</strong></article>
  <article class="metric"><span>市场成交额</span><strong>{_amount(market.get('total_amount'))}</strong></article>
  <article class="metric"><span>上涨 / 下跌</span><strong>{_integer(breadth.get('advancers'))} / {_integer(breadth.get('decliners'))}</strong></article>
</section>
<section class="panel"><h2>数据模块可用性</h2><div class="modules">{_readiness_cards(readiness)}</div></section>
<section class="panel"><h2>数据目录</h2><div class="scroll"><table><thead><tr><th>数据集</th><th>层级</th><th>路径</th><th>行数</th><th>SHA-256</th></tr></thead><tbody>{_catalog_rows(catalog)}</tbody></table></div></section>
<section class="two">
  <article class="panel"><h2>逐股字段覆盖率</h2><div class="scroll"><table><thead><tr><th>字段</th><th>非空</th><th>覆盖率</th><th>分布</th></tr></thead><tbody>{_coverage_rows(list(coverage.get('fields') or []))}</tbody></table></div></article>
  <article class="panel"><h2>申万一级事实汇总</h2><div class="scroll"><table><thead><tr><th>代码</th><th>行业</th><th>成分数</th><th>20日均值</th><th>20日中位数</th><th>上涨占比</th></tr></thead><tbody>{_industry_rows(industries)}</tbody></table></div></article>
</section>
<footer>数据截止时间：{_text(as_of.get('data_cutoff_time'))} · 页面与 <code>data_reference_latest.json</code> 使用同一源快照哈希。缺失字段保持为空，不做推断。</footer>
</main></body></html>"""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(page, encoding="utf-8", newline="\n")
    return output


__all__ = ["build_data_reference_dashboard"]
