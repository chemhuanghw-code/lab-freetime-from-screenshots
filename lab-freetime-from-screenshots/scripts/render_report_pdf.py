#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""课表 → 人员空闲 / 学时统计 PDF（A4 横排 · 非交互 · 可直接打印/发群）

共 4 页，一页一个主题，用 --pages 选择输出哪些（默认只出 3、4）：
  P1 概览     —— 指标卡、核心结论、跨组约束、三个方案摘要
  P2 排期方案 —— 推荐方案全周网格 + 两个备选方案时刻表
  P3 空闲学时 —— 每人空闲学时堆叠条形图 + 明细表
  P4 全周空闲表 —— 7 天 × 6 大节（名单 + 空闲人数）

**P3 / P4 里每位同学固定一种颜色 + 加粗**（见 PERSON_FIXED / PERSON_PALETTE），
便于跨页、跨格子追踪同一个人；色板不含红色系。

学时口径：1 学时 = 1 节课；1 个大节 = 2 学时；晚上第 9~12 节 = 4 学时。
全周总容量 84 学时（工作日白天 40 + 周末 16 + 晚修 28）。

用法:
  python render_report_pdf.py timetable_data.json --groups groups.json \
      --hoods hoods.json -o out --name 人员空闲与学时统计 --pages 3,4
"""
import argparse
import json
import os
import sys
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import Rectangle

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from render_freetime import _cjk_plt, week_dates          # noqa: E402
from schedule_experiments import (                        # noqa: E402
    best_plan, cost_of, describe, hood_short, is_night, load_hoods,
    load_inputs, max_clique, max_disjoint, short_slot,
)

# ------------------------------------------------------------------ 版式常量
W, H = 11.69, 8.27
INK, MUTED, HAIR = "#1f2328", "#6b7280", "#dce1e4"
TEAL_XD, TEAL, TEAL_M, TEAL_L, TEAL_XL = "#0B5345", "#0F6E56", "#1D9E75", "#9FE1CB", "#E6F4EF"
GRAY_TRACK = "#EAEDEF"
AMBER = "#B06F00"
RED = "#A32D2D"

GROUP_COLORS = [
    ("#E1F5EE", "#0F6E56"),
    ("#FAEEDA", "#854F0B"),
    ("#E6F1FB", "#185FA5"),
    ("#FBEAF0", "#993556"),
    ("#EEEDFE", "#534AB7"),
    ("#EAF3DE", "#3B6D11"),
    ("#FCEBEB", "#A32D2D"),
    ("#F1EFE8", "#444441"),
]

# 每人固定一种名字颜色（不含红色系，保证在浅底/浅绿底上都清晰）
# PERSON_FIXED：想给某人钉死某个颜色就写在这里，如 {"张三": "#185FA5"}。
# 不写则按名单顺序从 PERSON_PALETTE 取未占用的颜色，同一份报告里始终同色。
PERSON_FIXED = {}
PERSON_PALETTE = [
    "#185FA5",  # 蓝
    "#0F6E56",  # 墨绿
    "#534AB7",  # 紫
    "#854F0B",  # 棕
    "#1D7A8C",  # 青
    "#4C6B1F",  # 橄榄
    "#6B4E9E",  # 深紫
    "#A85A00",  # 橙（偏暖但不是红）
    "#2F6F4F",  # 松绿
    "#3E5D8A",  # 灰蓝
    "#5A5A2E",  # 卡其
    "#7A5C1E",  # 深金
]


def build_person_colors(names):
    """name → 固定色，同一份报告里同一个人始终同色。"""
    out, used = {}, set()
    for nm in names:
        c = PERSON_FIXED.get(nm)
        if c and c not in used:
            out[nm] = c
            used.add(c)
    pi = 0
    for nm in names:
        if nm in out:
            continue
        while PERSON_PALETTE[pi % len(PERSON_PALETTE)] in used:
            pi += 1
        c = PERSON_PALETTE[pi % len(PERSON_PALETTE)]
        out[nm] = c
        used.add(c)
        pi += 1
    return out

SLOT_TIMES = {
    "上午 1.2 节": "08:00–09:40",
    "上午 3.4 节": "10:00–11:40",
    "下午 5.6 节": "14:00–15:40",
    "下午 7.8 节": "16:00–17:40",
    "晚上 9.10 节": "18:30–20:10",
    "晚上 11.12 节": "20:20–22:00",
}


# ------------------------------------------------------------------ 画图小工具
def blank_page():
    fig = plt.figure(figsize=(W, H), dpi=200)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W)
    ax.set_ylim(0, H)
    ax.invert_yaxis()
    ax.axis("off")
    return fig, ax


def T(ax, x, y, s, fs=9, color=INK, bold=False, ha="center", va="center"):
    ax.text(x, y, s, fontsize=fs, color=color, ha=ha, va=va,
            weight="bold" if bold else "normal")


def R(ax, x, y, w, h, fc, ec="none", lw=0.8):
    ax.add_patch(Rectangle((x, y), w, h, facecolor=fc, edgecolor=ec, linewidth=lw))


def band(ax, title, sub="", right=""):
    R(ax, 0, 0, W, 0.94, TEAL_XD)
    R(ax, 0, 0.94, W, 0.045, TEAL_M)
    T(ax, 0.46, 0.36, title, 16, "white", True, ha="left")
    if sub:
        T(ax, 0.46, 0.70, sub, 9, "#b7e0d1", ha="left")
    if right:
        T(ax, W - 0.46, 0.36, right, 9, "#b7e0d1", ha="right")


def wrap_cn(s, n):
    out, cur = [], ""
    for ch in s:
        cur += ch
        if len(cur) >= n:
            out.append(cur)
            cur = ""
    if cur:
        out.append(cur)
    return out


def bullet_block(ax, x, y, w, items, fs=8.8, lh=0.208, gap=0.09, numbered=False):
    cy = y
    for i, it in enumerate(items):
        mark = f"{i+1}." if numbered else "·"
        T(ax, x, cy, mark, fs, TEAL_M if not numbered else TEAL, True, ha="left")
        for li, ln in enumerate(wrap_cn(it, max(10, int(w * 5.55)))):
            T(ax, x + 0.17, cy, ln, fs, INK, ha="left")
            cy += lh
        cy += gap
    return cy


def card(ax, x, y, w, h, label, value, note="", accent=TEAL_M):
    R(ax, x, y, w, h, "#F4F8F6")
    R(ax, x, y, 0.055, h, accent)
    T(ax, x + 0.22, y + 0.24, label, 8.5, MUTED, ha="left")
    T(ax, x + 0.22, y + h * 0.50, value, 19, INK, True, ha="left")
    if note:
        T(ax, x + 0.22, y + h - 0.22, note, 7.4, MUTED, ha="left")


def section_title(ax, x, y, s, sub=""):
    R(ax, x, y + 0.055, 0.05, 0.20, TEAL_M)
    T(ax, x + 0.16, y + 0.155, s, 11, INK, True, ha="left")
    if sub:
        T(ax, x + 0.16 + len(s) * 0.155 + 0.16, y + 0.155, sub, 8, MUTED, ha="left")
    ax.plot([x, W - 0.46], [y + 0.34, y + 0.34], color=HAIR, lw=0.8)


def kv_table(ax, x, y, w, headers, rows, col_w, row_h=0.235, hdr_h=0.27,
             fs=7.8, hdr_fs=7.8, aligns=None, hdr_bg=TEAL_XD, col_style=None):
    """col_style: {列号: fn(文本) -> (颜色, 是否加粗)}，用于给整列上固定色。"""
    aligns = aligns or ["left"] + ["center"] * (len(headers) - 1)
    col_style = col_style or {}
    cx = x
    R(ax, x, y, w, hdr_h, hdr_bg)
    for i, hd in enumerate(headers):
        a = aligns[i]
        tx = cx + 0.10 if a == "left" else cx + col_w[i] / 2
        T(ax, tx, y + hdr_h / 2, hd, hdr_fs, "white", True, ha=a)
        cx += col_w[i]
    cy = y + hdr_h
    for ri, row in enumerate(rows):
        if ri % 2 == 0:
            R(ax, x, cy, w, row_h, "#F8FAF9")
        cx = x
        for i, cell in enumerate(row):
            a = aligns[i]
            tx = cx + 0.10 if a == "left" else cx + col_w[i] / 2
            st = col_style.get(i)
            col, bold = (st(str(cell)) if st else (INK, False))
            T(ax, tx, cy + row_h / 2, str(cell), fs, col, bold, ha=a)
            cx += col_w[i]
        ax.plot([x, x + w], [cy + row_h, cy + row_h], color=HAIR, lw=0.6)
        cy += row_h
    return cy


def draw_names(ax, cx, col_w, ry, row_h, names, color_of, fs=8.4,
               gap=0.042, pad=0.10, bold=True):
    """格内逐名字上色：每人固定色 + 加粗；按可用宽度换行、每行居中。"""
    ch_w = fs / 72.0 * 1.03          # 单个汉字宽（英寸，含 3% 余量）
    max_w = col_w - 2 * pad
    lines, cur, cur_w = [], [], 0.0
    for nm in names:
        w = len(nm) * ch_w
        add = w if not cur else gap + w
        if cur and cur_w + add > max_w:
            lines.append(cur)
            cur, cur_w, add = [], 0.0, w
        cur.append(nm)
        cur_w += add
    if cur:
        lines.append(cur)
    if not lines:
        return
    step = min(0.215, (row_h - 0.26) / max(1, len(lines)))
    cy = ry + row_h / 2 + 0.09 - len(lines) * step / 2 + step / 2
    for line in lines:
        lw = sum(len(nm) * ch_w for nm in line) + gap * (len(line) - 1)
        x = cx + col_w / 2 - lw / 2
        for nm in line:
            ax.text(x, cy, nm, fontsize=fs, color=color_of.get(nm, INK),
                    ha="left", va="center", weight="bold" if bold else "normal")
            x += len(nm) * ch_w + gap
        cy += step


# ------------------------------------------------------------------ P1 概览
def page_overview(ctx):
    fig, ax = blank_page()
    ng = len(ctx["groups"])
    band(ax, ctx["report_title"],
         f"{ctx['week_hdr']}　·　{ctx['n_people']} 人 · {ng} 个组 · {len(ctx['hoods']) or '—'} 个工作位",
         "已纳入周末与晚修可选时段")

    cw, gap = 2.62, 0.17
    for i, (lab, val, note) in enumerate(ctx["metrics"]):
        card(ax, 0.46 + i * (cw + gap), 1.14, cw, 1.12, lab, val, note)

    section_title(ax, 0.46, 2.42, "核心结论")
    bullet_block(ax, 0.46, 2.92, 6.55, ctx["conclusions"])

    section_title(ax, 7.35, 2.42, "跨组人员", "（同一时段不能同时在两组）")
    rows = ctx["cross_rows"]
    kv_table(ax, 7.35, 2.92, 3.88, ["姓名", "组数", "所在组"], rows, [0.78, 0.52, 2.58],
             row_h=0.255, fs=7.6, aligns=["left", "center", "left"])
    y2 = 2.92 + 0.27 + 0.255 * len(rows) + 0.22
    R(ax, 7.35, y2, 3.88, 0.72, "#FDF6EC")
    T(ax, 7.52, y2 + 0.20, "为什么最少要 3 个时段", 8.4, AMBER, True, ha="left")
    for li, ln in enumerate(wrap_cn(ctx["clique_note"], 30)):
        T(ax, 7.52, y2 + 0.42 + li * 0.185, ln, 7.6, "#7a5a20", ha="left")

    section_title(ax, 0.46, 5.42, "三个可选方案", "（都在 3 个时段内做完 5 个组）")
    for i, p in enumerate(ctx["plans"]):
        x = 0.46 + i * (3.62 + 0.18)
        R(ax, x, 5.92, 3.62, 2.02, "#F7FAF9")
        R(ax, x, 5.92, 3.62, 0.34, TEAL if i == 0 else "#CFE3DA")
        T(ax, x + 0.16, 6.09, p["title"], 9.4, "white" if i == 0 else TEAL, True, ha="left")
        T(ax, x + 3.46, 6.09, p["tag"], 7.4, "white" if i == 0 else "#4b7a6a", ha="right")
        cy = 6.38
        for slot_txt, gtxt in p["lines"]:
            T(ax, x + 0.16, cy, slot_txt, 7.6, MUTED, ha="left")
            T(ax, x + 3.46, cy, gtxt, 8.2, INK, True, ha="right")
            cy += 0.245
            ax.plot([x + 0.16, x + 3.46], [cy - 0.075, cy - 0.075], color=HAIR, lw=0.6)
        T(ax, x + 0.16, 7.72, p["note"], 7.4, MUTED, ha="left")
    return fig


# ------------------------------------------------------------------ P2 排期方案
def draw_week_grid(ax, x0, y0, gw, gh, days, dates, slots, occ, groups, hood_of,
                   fs_day=8.4, fs_slot=7.6, fs_g=9.0, fs_h=6.4):
    """格内只放组名 + 通风橱；成员名单在下面图例里。"""
    nd, ns = len(days), len(slots)
    lab_w = gw * 0.145
    col_w = (gw - lab_w) / nd
    hdr_h = 0.34
    row_h = (gh - hdr_h) / ns

    R(ax, x0, y0, lab_w, hdr_h, TEAL_XD)
    T(ax, x0 + lab_w / 2, y0 + hdr_h / 2, "时段", fs_day, "white", True)
    for di, d in enumerate(days):
        cx = x0 + lab_w + di * col_w
        wk = di >= 5
        R(ax, cx, y0, col_w, hdr_h, TEAL_M if wk else TEAL_XD, ec="white", lw=1.2)
        T(ax, cx + col_w / 2, y0 + hdr_h * 0.36, d, fs_day, "white", True)
        T(ax, cx + col_w / 2, y0 + hdr_h * 0.74, dates[di], fs_day - 2.0, "#c8ecdf")

    for si, s in enumerate(slots):
        ry = y0 + hdr_h + si * row_h
        ev = "晚" in s
        R(ax, x0, ry, lab_w, row_h, TEAL_XL if ev else "#EAF3DE", ec="white", lw=1.2)
        T(ax, x0 + lab_w / 2, ry + row_h / 2 - 0.05, s.replace(" ", "").replace("节", ""),
          fs_slot, TEAL if ev else "#27500A", True)
        T(ax, x0 + lab_w / 2, ry + row_h / 2 + 0.12, SLOT_TIMES.get(s, ""), fs_slot - 2.6, MUTED)
        for di in range(nd):
            cx = x0 + lab_w + di * col_w
            lst = occ.get((di, si), [])
            if not lst:
                R(ax, cx + 0.02, ry + 0.02, col_w - 0.04, row_h - 0.04,
                  "#FAFBFB", ec="#E6EAED", lw=0.7)
                continue
            bh = (row_h - 0.05) / len(lst)
            for bi, gi in enumerate(sorted(lst)):
                by = ry + 0.025 + bi * bh
                bg, fg = GROUP_COLORS[gi % len(GROUP_COLORS)]
                R(ax, cx + 0.03, by, col_w - 0.06, bh, bg, ec=fg, lw=1.2)
                gname, mem = groups[gi]
                h = hood_of.get(gname)
                cyc = by + bh / 2
                drop = 0.085 if bh >= 0.33 else 0.0
                T(ax, cx + col_w / 2, cyc - drop, gname, fs_g, fg, True)
                if bh >= 0.30:
                    T(ax, cx + col_w / 2, cyc - drop + 0.155,
                      f"{len(mem)} 人 · " + ("通风橱 " + hood_short(h) if h else "无需通风橱"),
                      fs_h, "#5c6b64")


def draw_group_legend(ax, x0, y0, gw, groups, hood_of, ncol=5, fs=7.0):
    gap = 0.10
    cw = (gw - gap * (ncol - 1)) / ncol
    R(ax, x0, y0, gw, 0.62, "#F7FAF9")
    T(ax, x0 + 0.14, y0 + 0.14, "组员一览", 7.6, MUTED, ha="left")
    for i, (gname, mem) in enumerate(groups):
        cx = x0 + 0.14 + i * (cw + gap) - 0.14
        if i >= ncol:
            break
        bg, fg = GROUP_COLORS[i % len(GROUP_COLORS)]
        R(ax, cx, y0 + 0.26, 0.16, 0.16, bg, ec=fg, lw=0.9)
        T(ax, cx + 0.24, y0 + 0.34, gname, fs + 0.4, fg, True, ha="left")
        h = hood_of.get(gname)
        T(ax, cx, y0 + 0.50, "、".join(mem) + ("　[" + hood_short(h) + "]" if h else "　[无需通风橱]"),
          fs - 0.4, "#3d444c", ha="left")


def page_plans(ctx):
    fig, ax = blank_page()
    band(ax, "排期方案", "每个时段可并行多组：不撞人 + 不抢同一个工作位　·　姓名后带 * 为小组长",
         "工作位峰值占用见下图")
    p0 = ctx["plans"][0]

    section_title(ax, 0.46, 1.08, "推荐方案 · " + p0["title"], p0["note"])
    draw_week_grid(ax, 0.46, 1.52, 10.77, 3.30,
                   ctx["days5"], ctx["dates5"], ctx["slots4"], p0["occ"],
                   ctx["groups"], ctx["hood_of"])
    draw_group_legend(ax, 0.46, 4.94, 10.77, ctx["groups"], ctx["hood_of"])

    for i, p in enumerate(ctx["plans"][1:], start=1):
        x = 0.46 + (i - 1) * (5.44 + 0.11)
        y = 5.82
        R(ax, x, y, 5.44, 0.32, "#EAF3DE")
        T(ax, x + 0.15, y + 0.16, "备选" + ("一" if i == 1 else "二") + " · " + p["title"],
          9.2, "#27500A", True, ha="left")
        T(ax, x + 5.29, y + 0.16, p["tag"], 7.4, "#4b7a6a", ha="right")
        cy = y + 0.32
        for slot_txt, gtxt in p["lines"]:
            T(ax, x + 0.15, cy + 0.15, slot_txt, 7.8, INK, ha="left")
            T(ax, x + 5.29, cy + 0.15, gtxt, 8.0, MUTED, ha="right")
            cy += 0.30
            ax.plot([x + 0.15, x + 5.29], [cy, cy], color=HAIR, lw=0.6)
        T(ax, x + 0.15, cy + 0.20, p["note"], 7.4, MUTED, ha="left")
    return fig


# ------------------------------------------------------------------ P3 空闲学时
def page_hours(ctx):
    fig, ax = blank_page()
    band(ax, "人员空闲学时统计",
         "1 学时 = 1 节课　·　1 个大节 = 2 学时　·　晚上第 9~12 节 = 4 学时　·　"
         f"全周总容量 {ctx['total_capacity']} 学时"
         f"（工作日白天 {ctx['cap_wd']} + 周末 {ctx['cap_we']} + 晚修 {ctx['cap_nt']}）",
         "周末与晚修默认全员无课")

    axp = fig.add_axes([0.045, 0.375, 0.600, 0.430])
    axp.set_facecolor("none")
    people = ctx["hours"]
    ys = list(range(len(people)))
    TOTAL = ctx["total_capacity"]
    for i, p in enumerate(people):
        axp.barh(i, TOTAL, height=0.62, color=GRAY_TRACK, zorder=1)
        axp.barh(i, p["day_free"], height=0.62, color=TEAL, zorder=2)
        axp.barh(i, p["weekend_free"], left=p["day_free"], height=0.62, color=TEAL_M, zorder=3)
        axp.barh(i, p["night_free"], left=p["day_free"] + p["weekend_free"],
                 height=0.62, color=TEAL_L, zorder=4)
        axp.text(TOTAL + 1.4, i, f"{p['free']} 学时　空率 {p['rate']:.0f}%",
                 fontsize=7.6, va="center", ha="left", color=INK)
    axp.set_yticks(ys)
    axp.set_yticklabels([p["name"] for p in people], fontsize=8.8)
    for lab, p in zip(axp.get_yticklabels(), people):
        lab.set_color(ctx["name_color"].get(p["name"], INK))
        lab.set_fontweight("bold")
    axp.invert_yaxis()
    axp.set_xlim(0, TOTAL * 1.30)
    axp.set_xticks([0, 20, 40, 60, 84])
    axp.set_xticklabels(["0", "20", "40", "60", "84"], fontsize=7.6)
    axp.tick_params(axis="y", length=0)
    axp.set_xlabel("学时 / 周", fontsize=8.4, color=MUTED)
    for sp in ("top", "right", "left"):
        axp.spines[sp].set_visible(False)
    axp.spines["bottom"].set_color(HAIR)
    axp.grid(axis="x", color=HAIR, lw=0.6, zorder=0)
    axp.set_axisbelow(True)
    handles_legend = [
        (TEAL, f"工作日白天空闲（满 {ctx['cap_wd']}）"),
        (TEAL_M, f"周末空闲（满 {ctx['cap_we']}）"),
        (TEAL_L, f"晚修空闲（满 {ctx['cap_nt']}）"),
        (GRAY_TRACK, "有课（不可排）"),
    ]
    lx = 0.46
    for col, lab in handles_legend:
        R(ax, lx, 1.185, 0.20, 0.14, col, ec="#ccd4d8", lw=0.6)
        T(ax, lx + 0.27, 1.255, lab, 7.6, MUTED, ha="left")
        lx += 0.32 + len(lab) * 0.092 + 0.34

    section_title(ax, 0.46, 6.05, "明细", "（按合计空闲学时降序　·　姓名颜色与全周空闲表一致）")
    headers = ["姓名", "所属实验组", "有课学时", "工作日白天空闲", "周末空闲", "晚修空闲",
               "合计空闲学时", "空闲率"]
    col_w = [0.88, 2.40, 0.95, 1.52, 1.18, 1.18, 1.55, 1.11]
    rows = [[p["name"], p["groups"], p["busy_h"], p["day_free"], p["weekend_free"],
             p["night_free"], p["free"], f"{p['rate']:.0f}%"] for p in people]
    nc = ctx["name_color"]
    kv_table(ax, 0.46, 6.54, sum(col_w), headers, rows, col_w,
             row_h=0.142, hdr_h=0.24, fs=8.0, hdr_fs=7.6,
             aligns=["left", "left", "center", "center", "center", "center", "center", "center"],
             col_style={0: lambda s: (nc.get(s, INK), True)})
    return fig


# ------------------------------------------------------------------ P4 全周空闲表
def page_table(ctx):
    fig, ax = blank_page()
    band(ax, "全周人员空闲表",
         "格内 = 该时段空闲的同学（每人固定一种颜色、加粗）+ 空闲人数　·　含周末与晚修　·　灰底＝全员有课",
         f"{ctx['week']}")

    slots, days, dates = ctx["slots6"], ctx["days7"], ctx["dates7"]
    grid, total = ctx["grid"], ctx["n_people"]
    x0, y0 = 0.46, 1.28
    gw, gh = 10.77, 5.95
    lab_w = 1.06
    col_w = (gw - lab_w) / len(days)
    hdr_h = 0.46
    row_h = (gh - hdr_h) / len(slots)

    R(ax, x0, y0, lab_w, hdr_h, TEAL_XD)
    T(ax, x0 + lab_w / 2, y0 + hdr_h / 2, "时段", 8.6, "white", True)
    for di, d in enumerate(days):
        cx = x0 + lab_w + di * col_w
        wk = di >= 5
        R(ax, cx, y0, col_w, hdr_h, TEAL_M if wk else TEAL_XD, ec="white", lw=1.2)
        T(ax, cx + col_w / 2, y0 + hdr_h * 0.34, d, 8.6, "white", True)
        T(ax, cx + col_w / 2, y0 + hdr_h * 0.72, dates[di], 7.4, "#c8ecdf")

    for si, s in enumerate(slots):
        ry = y0 + hdr_h + si * row_h
        ev = "晚" in s
        R(ax, x0, ry, lab_w, row_h, TEAL_XL if ev else "#EAF3DE", ec="white", lw=1.2)
        T(ax, x0 + lab_w / 2, ry + row_h / 2 - 0.07, s.replace(" ", "").replace("节", ""),
          7.8, TEAL if ev else "#27500A", True)
        T(ax, x0 + lab_w / 2, ry + row_h / 2 + 0.11, SLOT_TIMES.get(s, ""), 6.0, MUTED)
        for di in range(len(days)):
            cx = x0 + lab_w + di * col_w
            names = grid[si][di]
            n = len(names)
            if n == 0:
                R(ax, cx + 0.02, ry + 0.02, col_w - 0.04, row_h - 0.04, "#F1F2F4", ec="#E1E5E8", lw=0.7)
                T(ax, cx + col_w / 2, ry + row_h / 2, "全员有课", 8.4, "#6b7280", True)
                continue
            full = (n == total)
            R(ax, cx + 0.02, ry + 0.02, col_w - 0.04, row_h - 0.04,
              "#D6F0E2" if full else "#FBFDFC", ec="#CFE7DC" if full else "#E1E5E8", lw=0.7)
            if si >= 0 and di >= 5:
                R(ax, cx + 0.02, ry + 0.02, col_w - 0.04, row_h - 0.04, "#D6F0E2", ec="#CFE7DC", lw=0.7)
            if full:
                T(ax, cx + col_w / 2, ry + row_h / 2, f"全员空闲　{total} 人", 8.4, "#0F6E56", True)
                continue
            T(ax, cx + col_w - 0.14, ry + 0.19, str(n), 9.2, TEAL, True, ha="right")
            draw_names(ax, cx, col_w, ry, row_h, names, ctx["name_color"], fs=8.4)

    ly = y0 + gh + 0.16
    R(ax, x0, ly, 0.22, 0.13, "#D6F0E2", ec="#CFE7DC", lw=0.7)
    T(ax, x0 + 0.30, ly + 0.065, "全员空闲", 7.2, MUTED, ha="left")
    R(ax, x0 + 1.55, ly, 0.22, 0.13, "#FBFDFC", ec="#E1E5E8", lw=0.7)
    T(ax, x0 + 1.85, ly + 0.065, "部分空闲（右上角数字＝空闲人数）", 7.2, MUTED, ha="left")
    R(ax, x0 + 5.05, ly, 0.22, 0.13, "#F1F2F4", ec="#E1E5E8", lw=0.7)
    T(ax, x0 + 5.35, ly + 0.065, "全员有课", 7.2, MUTED, ha="left")
    T(ax, W - 0.46, ly + 0.065, "周末与晚修默认全员无课，可作为实验的备用时段", 7.2, MUTED, ha="right")
    return fig


# ------------------------------------------------------------------ 数据准备
def build_context(data_path, groups_path, hoods_path):
    if groups_path and os.path.exists(groups_path):
        meta, slots, days, busy, groups = load_inputs(data_path, groups_path, 0)
    else:
        # 没给分组也能出 PDF：每人一个单人组（"所属实验组"列退化为人名，不影响空闲学时与空闲表）
        with open(data_path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
        meta, slots, days = raw.get("meta", {}), raw["slots"], raw["days"]
        busy = {p["name"]: {(int(d), int(s)) for d, s in p["busy"]} for p in raw["people"]}
        groups = [(nm, [nm]) for nm in busy]
    hoods, hood_of = load_hoods(hoods_path, [g for g, _m in groups])
    nd, ns = len(days), len(slots)

    day_slots = [k for k in range(1, ns + 1) if not is_night(slots[k - 1])]
    night_slots = [k for k in range(1, ns + 1) if is_night(slots[k - 1])]
    # 三类时段分开计：工作日白天 / 周末白天 / 晚修（晚修含周末晚上）
    wd_set = {(d, s) for d in range(1, min(5, nd) + 1) for s in day_slots}
    we_set = {(d, s) for d in range(6, nd + 1) for s in day_slots}
    nt_set = {(d, s) for d in range(1, nd + 1) for s in night_slots}
    weekday_day = wd_set          # 兼容旧字段名

    memsets = [set(m) for _g, m in groups]
    n = len(groups)
    clq = max_clique(memsets, hood_of, groups, n)
    dis = max_disjoint(memsets, hood_of, groups, n)

    def solve_for(day_list, slot_list, kmin):
        free = {nm: {(d, s) for d in day_list for s in slot_list if (d, s) not in bs}
                for nm, bs in busy.items()}
        cand = {}
        for gi, (_g, mem) in enumerate(groups):
            known = [m for m in mem if m in free]
            inter = set(free[known[0]]) if known else set()
            for m in known[1:]:
                inter &= free[m]
            cand[gi] = sorted(inter, key=lambda x: (x[0], x[1]))
        if not all(cand[i] for i in range(n)):
            return None, None, cand
        sol, cost = best_plan(groups, cand, hood_of,
                              {s for s in slot_list if is_night(slots[s - 1])}, kmin=kmin)
        return sol, cost, cand

    def plan_card(title, tag, day_list, slot_list, note, occ_days, occ_slots, kmin=3):
        sol, cost, _cand = solve_for(day_list, slot_list, kmin)
        if sol is None:
            return None
        by = defaultdict(list)
        for gi, (d, s) in sol.items():
            by[(d, s)].append(gi)
        lines = []
        for (d, s) in sorted(by):
            gn = " ＋ ".join(groups[gi][0] for gi in sorted(by[(d, s)]))
            lines.append((f"{days[d - 1]} {short_slot(slots[s - 1])}", gn))
        occ = defaultdict(list)
        for gi, (d, s) in sol.items():
            occ[(d - 1, s - 1)].append(gi)
        return {
            "title": title, "tag": tag, "note": note, "lines": lines,
            "occ": dict(occ), "sol": sol, "cost": cost,
            "days": day_list, "slots": slot_list,
        }

    p_a = plan_card("A · 工作日白天", "不占周末晚修", list(range(1, 6)), day_slots,
                    "推荐：最省时间，也不占用休息日", 5, len(day_slots), kmin=len(clq))
    p_b = plan_card("B · 全部排晚修", "白天一节不占", list(range(1, nd + 1)), night_slots,
                    "适合白天满课、只能晚上进实验室", nd, len(night_slots), kmin=len(clq))
    p_c = plan_card("C · 周末集中", "两天做完全部", [6, 7], day_slots + night_slots,
                    "适合不想影响上课的同学", 2, len(day_slots) + len(night_slots), kmin=len(clq))

    plans = [p for p in (p_a, p_b, p_c) if p]

    # ---- 学时统计（工作日白天 / 周末 / 晚修 分开算）
    n_wd, n_we, n_nt = len(wd_set), len(we_set), len(nt_set)
    total_cap_ = (n_wd + n_we + n_nt) * 2
    hours = []
    for nm in busy:
        b_wd = len(busy[nm] & wd_set)
        b_we = len(busy[nm] & we_set)
        b_nt = len(busy[nm] & nt_set)
        wd_free, we_free, nt_free = (n_wd - b_wd) * 2, (n_we - b_we) * 2, (n_nt - b_nt) * 2
        total_free = wd_free + we_free + nt_free
        hours.append({
            "name": nm, "busy_h": (b_wd + b_we + b_nt) * 2,
            "day_free": wd_free, "weekend_free": we_free, "night_free": nt_free,
            "other_free": we_free + nt_free,
            "free": total_free, "rate": 100.0 * total_free / total_cap_,
            "groups": "、".join(g for g, m in groups if nm in m) or "—",
        })
    hours.sort(key=lambda x: (-x["free"], x["name"]))
    total_cap = total_cap_

    # ---- 全周空闲表 grid（si, di）→ 空闲名单
    grid = []
    for si in range(ns):
        row = []
        for di in range(nd):
            names = [nm for nm, bs in busy.items() if (di + 1, si + 1) not in bs]
            row.append(names)
        grid.append(row)

    # ---- 跨组表
    belong = defaultdict(list)
    for gname, mem in groups:
        for m in mem:
            belong[m].append(gname)
    cross = sorted(((m, g) for m, g in belong.items() if len(g) > 1),
                   key=lambda kv: (-len(kv[1]), kv[0]))
    cross_rows = [[m, len(g), "、".join(g)] for m, g in cross]

    dates = week_dates(meta.get("monday_date", ""), days)

    wk_no = str(meta.get("week_no", "") or "").strip()
    wk_hdr = (wk_no if wk_no.startswith("第") else f"第 {wk_no} 周") if wk_no else ""
    if meta.get("week_label"):
        wk_hdr += (f"　{meta['week_label']}" if wk_hdr else str(meta["week_label"]))

    # 结论与「为什么最少要 N 个时段」全部由数据推出来，不写死任何姓名/组名
    cross_ordered = [m for m, _g in cross]
    n_hoods = len(hoods)
    clique_note = (
        f"有 {len(clq)} 个组两两不能同时开（共享成员或抢同一个工作位），"
        "它们必须各占一个时段 —— 这就是时段数的下限。"
        if clq else "各组之间没有硬冲突，可以完全并行。"
    )

    ctx = {
        "week": meta.get("week_label", ""),
        "week_no": meta.get("week_no", ""),
        "week_hdr": wk_hdr,
        "report_title": meta.get("report_title") or "实验排期方案",
        "slots6": slots, "days7": days, "dates7": dates,
        "slots4": slots[:len(day_slots)], "days5": days[:5], "dates5": dates[:5],
        "groups": groups, "hood_of": hood_of, "hoods": hoods,
        "busy": busy, "grid": grid, "n_people": len(busy),
        "name_color": build_person_colors(list(busy.keys())),
        "hours": hours, "total_capacity": total_cap,
        "cap_wd": n_wd * 2, "cap_we": n_we * 2, "cap_nt": n_nt * 2,
        "plans": plans,
        "metrics": [
            ("实验组", f"{n} 个", f"{len(busy)} 人 · {len(cross)} 人跨组"),
            ("工作位", f"{n_hoods} 个" if n_hoods else "—",
             "、".join(hood_short(h) for h in hoods) or "未配置"),
            ("排完所需时段", f"{plans[0]['cost'][0]} 个" if plans else "—",
             f"串行要 {n} 个" + (f"，省 {n - plans[0]['cost'][0]} 个" if plans else "")),
            ("全周总容量", f"{total_cap} 学时", f"{nd} 天 × ({len(day_slots)} 节白天 + {len(night_slots)} 节晚)"),
        ],
        "conclusions": ctx_conclusions(cross_ordered, plans, n, n_hoods, clq),
        "cross_rows": cross_rows,
        "clique_note": clique_note,
    }
    return ctx


def ctx_conclusions(cross_ordered, plans, n_groups, n_hoods, clq):
    """结论由数据生成，避免把某一批具体数据写死在代码里。"""
    out = []
    if plans:
        k = plans[0]["cost"][0]
        out.append(f"{n_groups} 个组全部排完只需 {k} 个时段（串行排要 {n_groups} 个）。")
    if cross_ordered:
        who = cross_ordered[0]
        cnt = sum(1 for m in cross_ordered if m == who)
        out.append(f"跨组的人是主要约束：{who} 同时在多个组里，"
                   f"这些组两两不能同时开，直接决定了时段数的下限。")
    else:
        out.append("没有人跨组，各组可完全独立排期。")
    out.append(f"工作位 {n_hoods} 个，通常不是瓶颈 —— 真正的瓶颈是跨组的人。" if n_hoods
               else "未配置工作位约束，排期只受人员时间限制。")
    out.append("周末与晚修默认全员无课，是最可靠的备用池：把实验往后挪一天的成本很低。")
    out.append("三个方案都给出最少时段解：A 不占周末晚修、B 全排晚修、C 周末集中。"
               "按你能到场的时间挑一个即可。")
    return out


# ------------------------------------------------------------------ 主流程
PAGES = {
    "1": ("概览", page_overview),
    "2": ("排期方案", page_plans),
    "3": ("空闲学时", page_hours),
    "4": ("全周空闲表", page_table),
}


def default_report_name(ctx):
    """默认文件名：第N周_日期段_人员空闲与学时统计（周次或日期缺失就跳过该段）"""
    bad = '\\/:*?"<>|'
    def clean(s):
        s = str(s or "").strip()
        for c in bad:
            s = s.replace(c, "")
        return s
    wk = clean(ctx.get("week_no", "")).strip("第周").strip()
    wk = f"第{wk}周" if wk else ""
    seg = clean(ctx.get("week", ""))
    return "_".join([p for p in (wk, seg, "人员空闲与学时统计") if p])


def main():
    ap = argparse.ArgumentParser(description="课表 → 人员空闲 / 学时统计 PDF")
    ap.add_argument("data")
    ap.add_argument("--groups", default=None,
                    help="实验分组 JSON；不给则按「每人一组」出空闲与学时统计")
    ap.add_argument("--hoods", default=None)
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--name", default=None,
                    help="输出文件名（不含扩展名）。默认自动带「第N周 + 日期段」，"
                         "如 第3周_9.14-9.20_人员空闲与学时统计.pdf")
    ap.add_argument("--pages", default="3,4",
                    help="输出哪些页：1=概览 2=排期方案 3=空闲学时 4=全周空闲表；"
                         "默认 3,4；传 all 输出全部四页")
    ap.add_argument("--preview", default=None,
                    help="额外把每页存成 PNG（给目录），便于检查版式")
    args = ap.parse_args()

    _cjk_plt()
    ctx = build_context(args.data, args.groups, args.hoods)

    if args.pages.strip().lower() in ("all", "*"):
        seq = [(k, PAGES[k][0], PAGES[k][1]) for k in ("1", "2", "3", "4")]
    else:
        keys = [k.strip() for k in args.pages.replace("，", ",").split(",") if k.strip()]
        seq = [(k, PAGES[k][0], PAGES[k][1]) for k in keys if k in PAGES]
    if not seq:
        seq = [("3", PAGES["3"][0], PAGES["3"][1]), ("4", PAGES["4"][0], PAGES["4"][1])]

    outdir = args.out or os.path.dirname(os.path.abspath(args.data))
    os.makedirs(outdir, exist_ok=True)
    name = args.name or default_report_name(ctx)
    path = os.path.join(outdir, name + ".pdf")

    with PdfPages(path) as pdf:
        for k, title, fn in seq:
            fig = fn(ctx)
            pdf.savefig(fig)
            if args.preview:
                os.makedirs(args.preview, exist_ok=True)
                fig.savefig(os.path.join(args.preview, f"P{k}.png"),
                            dpi=110, facecolor="white")
            plt.close(fig)

    # 控制台摘要
    print(f"已生成：{path}")
    print(f"页数：{len(seq)}（" + "、".join(f"P{k} {t}" for k, t, _f in seq) + "）")
    print(f"人数：{ctx['n_people']}　·　组数：{len(ctx['groups'])}")
    print("姓名固定色：" + "　".join(f"{k}={v}" for k, v in ctx["name_color"].items()))
    print("空闲学时（由多到少）：")
    for h in ctx["hours"]:
        print(f"  {h['name']:<4}合计 {h['free']:>3} 学时  空率 {h['rate']:.0f}%"
              f"　（工作日白天 {h['day_free']} + 周末 {h['weekend_free']} + 晚修 {h['night_free']}"
              f"，有课 {h['busy_h']}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
