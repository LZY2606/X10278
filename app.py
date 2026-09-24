"""FastAPI review UI for the Matroska timecode evidence case.

One visible path: EBML tree -> cluster ranges -> track timelines -> cue
pointers -> corrupt byte ranges -> anomaly source chain -> judgments ->
branch compare -> revert. All panels are server-rendered SVG/HTML.
"""
from __future__ import annotations

import html
import os
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

import casepack
import service
from store import ConflictError, Store

BASE_DIR = Path(__file__).resolve().parent
FIXTURE_DIR = BASE_DIR / "fixtures"
DB_PATH = os.environ.get("MKV_DB", str(BASE_DIR / "app.db"))

app = FastAPI(title="Matroska 时码庭")


def get_store() -> Store:
    FIXTURE_DIR.mkdir(exist_ok=True)
    casepack.ensure_fixtures(FIXTURE_DIR)
    return Store(DB_PATH)


CSS = """
body{font-family:-apple-system,Helvetica,Arial,sans-serif;margin:24px;color:#222}
h1{font-size:22px} h2{font-size:16px;margin:18px 0 6px}
table{border-collapse:collapse;font-size:12px;margin:6px 0}
td,th{border:1px solid #ccc;padding:3px 8px;vertical-align:top}
th{background:#f0f0f0}
.panel{border:1px solid #ddd;border-radius:6px;padding:10px;margin:10px 0}
button{padding:3px 10px}
.sev-error{color:#b00020;font-weight:bold}
.sev-warn{color:#b06000;font-weight:bold}
.sev-info{color:#226622}
code{background:#f6f6f6;padding:1px 4px}
svg text{font-family:Menlo,monospace}
.small{font-size:11px;color:#666}
"""


def esc(value) -> str:
    return html.escape(str(value))


def page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{esc(title)}</title><style>{CSS}</style></head><body>"
        f"<h1>{esc(title)}</h1>{body}</body></html>")


# ---------- SVG panels ----------

def svg_ebml_tree(elements) -> str:
    rows = []

    def walk(e, depth):
        rows.append((depth, e))
        for c in e["children"]:
            walk(c, depth + 1)

    for e in elements:
        walk(e, 0)
    height = 20 * len(rows) + 10
    parts = [f"<svg width='980' height='{height}' "
             "xmlns='http://www.w3.org/2000/svg'>"]
    y = 18
    for depth, e in rows:
        x = 8 + depth * 18
        unknown = " [未知长度]" if e["unknown_size"] else ""
        label = (f"{e['name']} @{e['offset']}..{e['end']}"
                 f" hdr={e['header_size']}{unknown}")
        if e.get("value") is not None:
            label += f" = {e['value']}"
        color = "#b06000" if e["unknown_size"] else "#222"
        parts.append(
            f"<text x='{x}' y='{y}' font-size='11' fill='{color}'>"
            f"<title>{esc(e['id'])}</title>{esc(label)}</text>")
        y += 20
    parts.append("</svg>")
    return "".join(parts)


def svg_clusters(payload) -> str:
    nodes = payload["nodes"]
    total = payload["source_len"]
    width, left = 900, 40
    scale = width / max(total, 1)
    clusters = [nodes[k] for k in sorted(nodes) if k.startswith("cluster:")]
    parts = ["<svg width='980' height='150' "
             "xmlns='http://www.w3.org/2000/svg'>",
             f"<line x1='{left}' y1='60' x2='{left + width}' y2='60'"
             " stroke='#999'/>"]
    for c in clusters:
        x = left + c["offset"] * scale
        w = max((c["end"] - c["offset"]) * scale, 2)
        fill = "#ffd27f" if c["unknown_size"] else "#9fd3a8"
        parts.append(
            f"<rect x='{x:.1f}' y='40' width='{w:.1f}' height='40'"
            f" fill='{fill}' stroke='#555'><title>{c['id']}</title></rect>"
            f"<text x='{x:.1f}' y='34' font-size='10'>{c['id']}"
            f" tc={c['timecode']}</text>")
        for b in c["blocks"]:
            if b["lacing_corrupt"]:
                bx = left + b["raw_range"][0] * scale
                bw = max((b["raw_range"][1] - b["raw_range"][0]) * scale, 2)
                parts.append(
                    f"<rect x='{bx:.1f}' y='40' width='{bw:.1f}' height='40'"
                    f" fill='rgba(200,0,0,0.55)'><title>{b['id']}"
                    f" 损坏字节范围</title></rect>")
    for a in nodes["anomalies"]:
        if a["kind"] == "cluster_gap":
            left_c = nodes[a["object_refs"][0]]
            gx = left + left_c["end"] * scale
            parts.append(
                f"<line x1='{gx:.1f}' y1='30' x2='{gx:.1f}' y2='90'"
                f" stroke='#b06000' stroke-dasharray='4 3'>"
                f"<title>{a['id']}</title></line>"
                f"<text x='{gx + 3:.1f}' y='100' font-size='10'"
                f" fill='#b06000'>时码缺口</text>")
    parts.append(f"<text x='{left}' y='125' font-size='10' fill='#666'>"
                 f"字节轴 0..{total}；橙=未知长度，红=损坏字节范围</text>")
    parts.append("</svg>")
    return "".join(parts)


def svg_timelines(payload) -> str:
    tracks = payload["nodes"]["tracks"]["tracks"]
    max_ns = 1
    for t in tracks:
        for f in t["frames"]:
            if f["status"] == "ok" and f["times_ns"]:
                max_ns = max(max_ns, f["times_ns"][-1])
            else:
                max_ns = max(max_ns, f.get("start_ns") or 0)
    width, left, top = 900, 60, 20
    lane_h = 46
    scale = width / max_ns
    height = top + lane_h * len(tracks) + 40
    parts = [f"<svg width='980' height='{height}' "
             "xmlns='http://www.w3.org/2000/svg'>"]
    y = top
    for t in tracks:
        parts.append(
            f"<text x='4' y='{y + 14}' font-size='11'><title>{t['id']}"
            f"</title>{esc(t['id'])}</text>"
            f"<text x='4' y='{y + 28}' font-size='9' fill='#777'>"
            f"dur={t['default_duration_ns']}ns</text>"
            f"<line x1='{left}' y1='{y + 18}' x2='{left + width}'"
            f" y2='{y + 18}' stroke='#ccc'/>")
        for f in t["frames"]:
            if f["status"] == "ok":
                for ts in f["times_ns"]:
                    x = left + ts * scale
                    parts.append(
                        f"<line x1='{x:.1f}' y1='{y + 10}' x2='{x:.1f}'"
                        f" y2='{y + 26}' stroke='#226'><title>{f['block']}"
                        f" {ts}ns</title></line>")
            else:
                x = left + (f.get("start_ns") or 0) * scale
                parts.append(
                    f"<rect x='{x:.1f}' y='{y + 8}' width='14' height='20'"
                    f" fill='#ddd' stroke='#b00020'"
                    f" stroke-dasharray='3 2'><title>{f['block']}"
                    f" {esc(f['diagnostic'])}</title></rect>"
                    f"<text x='{x + 2:.1f}' y='{y + 23}' font-size='11'"
                    f" fill='#b00020'>?</text>")
        y += lane_h
    parts.append(f"<text x='{left}' y='{y + 16}' font-size='10' fill='#666'>"
                 f"时间轴 0..{max_ns} ns；每轨独立时基，"
                 "不按文件顺序强对齐</text></svg>")
    return "".join(parts)


def svg_cues(payload) -> str:
    nodes = payload["nodes"]
    cues = nodes["cues"]["cues"]
    max_ns = 1
    for c in cues:
        max_ns = max(max_ns, c["cue_time_ns"])
        for p in c["positions"]:
            if p["actual_timecode_ns"]:
                max_ns = max(max_ns, p["actual_timecode_ns"])
    width, left = 900, 60
    scale = width / max_ns
    height = 40 + 44 * len(cues)
    parts = [f"<svg width='980' height='{height}' "
             "xmlns='http://www.w3.org/2000/svg'>"]
    y = 30
    for c in cues:
        cx = left + c["cue_time_ns"] * scale
        color = "#226" if c["status"] == "ok" else "#b00020"
        parts.append(
            f"<circle cx='{cx:.1f}' cy='{y}' r='5' fill='{color}'>"
            f"<title>{c['id']} 声明 {c['cue_time_ns']}ns</title></circle>"
            f"<text x='{cx + 8:.1f}' y='{y + 4}' font-size='10'>"
            f"{c['id']} 声明 {c['cue_time_ns']}ns</text>")
        for p in c["positions"]:
            if p["actual_timecode_ns"] is None:
                continue
            tx = left + p["actual_timecode_ns"] * scale
            parts.append(
                f"<line x1='{cx:.1f}' y1='{y}' x2='{tx:.1f}' y2='{y}'"
                f" stroke='{color}' stroke-dasharray='4 3'"
                f" marker-end='url(#arr)'/>"
                f"<rect x='{tx - 3:.1f}' y='{y - 5}' width='6' height='10'"
                f" fill='#9fd3a8'><title>{p['hit_cluster']} 解析"
                f" {p['actual_timecode_ns']}ns</title></rect>")
        y += 44
    parts.insert(1, "<defs><marker id='arr' markerWidth='8'"
                    " markerHeight='8' refX='6' refY='3' orient='auto'>"
                    "<path d='M0,0 L6,3 L0,6 z' fill='#666'/></marker></defs>")
    parts.append("</svg>")
    return "".join(parts)
