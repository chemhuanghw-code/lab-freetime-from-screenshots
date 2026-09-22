#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把一批课表 App 截图直接转成 timetable_data.json（无需 OCR）。

原理：这类课表 App 的「有课」格子都填了彩色块，只判定「哪个格子有课」即可，
生成人员空闲表本来也不需要课程名。所以可以全自动、零识别误差。

版面规律（已在「标准版」App 的多种机型和缩放下验证）：
  * 表头是蓝色条，其底边 = 课表网格顶（grid_top）
  * 左侧「一大节/01,02」标签列宽 ≈ 屏宽 10%
  * 右侧按星期等分；列数 N 用表头白色「周一/21」文字簇的间距反推
    （有的截图只截到周五，所以要自适应 5/6/7 列）
  * 行高用左侧两行标签「一大节」+「01,02」的垂直中心间距确定
  * 底部「无课表课程」面板会盖住最后一行，需先求出面板顶边并裁掉

⚠ 星期排列不一定是「周一在前」！少数截图是「周日在前」（日期行首列写「周日 13」，
  末列才是「周六 19」）。像素分析只看列的位置、读不出星期，所以这种图会把整周
  错位一天。交付前务必裁出日期行肉眼核对（见 SKILL.md「核对表头」），发现异常用
  --first-day 纠正，例如 --first-day "某人=周日"。

用法:
  python shots_to_json.py <截图目录> [-o out.json] [--week 9.21-9.27] [--monday 9.21]
  python shots_to_json.py <截图目录> -o out.json --first-day "某人=周日"   # 该图周日在前
  python shots_to_json.py <截图目录> -o out.json --first-day 周日         # 全部图都周日在前
"""
import argparse
import json
import os
import re
import sys

import numpy as np
from PIL import Image

DAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
# 6 个大节：白天 4 + 晚上 4 节课（第 9~12 节，拆成 2 个大节）
SLOTS = ["上午 1.2 节", "上午 3.4 节", "下午 5.6 节", "下午 7.8 节",
         "晚上 9.10 节", "晚上 11.12 节"]
# 截图上只有 5 行（白天 4 行 + 「晚上」1 行）；第 5 行覆盖最后两个大节
ROW2SLOT = {1: [1], 2: [2], 3: [3], 4: [4], 5: [5, 6]}
N_ROWS = 5
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
BUSY_THR = 0.55
# 同目录里可能混着汇总表/统计表，按文件名排除，别当学生课表处理
SKIP_FN = ("统计表", "汇总表", "汇总", "总表", "空闲表", "人员表")


def _merge(vals, gap):
    out = []
    for v in vals:
        if out and v - out[-1][-1] <= gap:
            out[-1].append(v)
        else:
            out.append([v])
    return out


def find_layout(a):
    """返回 dict(grid_top, panel_top, rows, cols, n_days, h_row)"""
    H, W, _ = a.shape
    mx = a.max(2)
    blue = (a[:, :, 2] > a[:, :, 0] + 20) & (a[:, :, 2] > 120) & (a[:, :, 2] < 245)
    bfrac = blue.mean(1)

    lo, hi = int(H * 0.03), int(H * 0.48)
    cand = [y for y in range(lo, hi) if bfrac[y] > 0.5]
    grid_top = (max(cand) + 1) if cand else int(H * 0.22)

    # 底部「无课表课程」面板顶边
    panel_top = H
    for y in range(grid_top + 20, H):
        if bfrac[y] > 0.35:
            panel_top = y
            break

    # ---- 行：左侧标签两行文字的垂直中心
    dark = mx < 160
    lab = dark[:, : int(W * 0.13)].sum(1)
    gs = _merge([y for y in np.where(lab > 2)[0] if y > grid_top], 60)
    centers = [(g[0] + g[-1]) / 2 for g in gs
               if len(g) >= 8 and (g[0] + g[-1]) / 2 < panel_top - 5]
    if len(centers) >= 2:
        diffs = [centers[i + 1] - centers[i] for i in range(len(centers) - 1)]
        h_row = float(np.median(diffs))
        first = centers[0]
    else:
        h_row = (min(panel_top, int(H * 0.75)) - grid_top) / 5.0
        first = grid_top + h_row / 2
    rows = [(first + h_row * i - h_row / 2, first + h_row * i + h_row / 2) for i in range(N_ROWS)]

    # ---- 列：表头白字簇 -> 间距 -> 列数
    y = grid_top - 1
    while y > 0 and bfrac[y] > 0.5:
        y -= 1
    hdr = a[y + 1:grid_top]
    white = (hdr.min(2) > 180)
    hist = white.sum(0)
    on = hist > max(2, hist.max() * 0.12)
    grp, s = [], None
    for x in range(W):
        if on[x] and s is None:
            s = x
        elif not on[x] and s is not None:
            if x - s > 4:
                grp.append([s, x])
            s = None
    merged = []
    for g in grp:
        if merged and g[0] - merged[-1][1] < 0.025 * W:
            merged[-1][1] = g[1]
        else:
            merged.append(g)
    centers_x = [int((p + q) / 2) for p, q in merged]
    diffs = [centers_x[i + 1] - centers_x[i] for i in range(len(centers_x) - 1)]
    diffs = [d for d in diffs if 0.05 * W < d < 0.32 * W]
    lab_right = int(W * 0.10)
    if diffs:
        spacing = float(np.median(diffs))
        n_days = int(round((W - lab_right) / spacing))
    else:
        spacing, n_days = (W - lab_right) / 7.0, 7
    n_days = max(1, min(7, n_days))
    col_w = (W - lab_right) / n_days
    cols_ = [(lab_right + j * col_w, lab_right + (j + 1) * col_w) for j in range(n_days)]

    return {"grid_top": grid_top, "panel_top": panel_top, "rows": rows, "cols": cols_,
            "n_days": n_days, "h_row": h_row, "spacing": spacing}


def busy_cells(path, debug=False):
    a = np.asarray(Image.open(path).convert("RGB")).astype(np.int16)
    mx = a.max(2)
    mn = a.min(2)
    sat = mx - mn
    # 关键：这批 App 的色块是浅色系，最大通道常达 250~252，
    # 不能用「接近纯白」排除，只能用饱和度排除白底（白底 sat≈0）
    colored = (sat > 30) & (mx > 60)
    lay = find_layout(a)
    gt, pt, rows, cols_, h_row = lay["grid_top"], lay["panel_top"], lay["rows"], lay["cols"], lay["h_row"]

    busy, frac = set(), {}
    for i, (y0, y1) in enumerate(rows):
        for j, (x0, x1) in enumerate(cols_):
            cx0, cx1 = int(x0 + 4), int(x1 - 4)
            cy0 = int(y0 + h_row * 0.18)
            cy1 = int(min(y1 - h_row * 0.06, pt - 3))
            blk = colored[max(cy0, 0):max(cy1, 0), max(cx0, 0):max(cx1, 0)]
            f = float(blk.mean()) if blk.size else 0.0
            frac[(j + 1, i + 1)] = round(f, 2)
            if f > BUSY_THR:
                for sl in ROW2SLOT.get(i + 1, [i + 1]):   # 第 5 行「晚上」覆盖两个大节
                    busy.add((j + 1, sl))
    if debug:
        print(f"     网格顶={gt} 面板顶={pt} 行高={h_row:.0f} 列距={lay['spacing']:.0f} 列数={lay['n_days']}")
        for i in range(5):
            print("     行%d " % (i + 1) +
                  " ".join(f"{frac.get((j+1, i+1), 0):.2f}" for j in range(lay["n_days"])))
    return busy, lay, frac


STRIP = ["的课表", "课程表", "课表截图", "截图", "的某一周", "某一周", "空闲表", "空闲", "课表"]
SEP = re.compile(r"[-_+＋,，、\s]+")


def names_from_file(fn):
    """从文件名取姓名，支持一张图多个人（同班共用一张课表）。

    `张三-李四-王五-3.jpg` -> ["张三", "李四", "王五"]
    `张三的某一周课表截图.jpg`  -> ["张三"]
    """
    s = os.path.splitext(os.path.basename(fn))[0]
    s = re.sub(r"第\s*\d+\s*周", "", s)
    for k in STRIP:
        s = s.replace(k, "")
    names = []
    for p in SEP.split(s):
        p = re.sub(r"[（(]\s*\d+\s*[)）]", "", p).strip()
        if not p or re.fullmatch(r"[\d.\-]*", p):      # 纯数字＝周次/序号
            continue
        names.append(p)
    return names or [os.path.splitext(os.path.basename(fn))[0]]


def main():
    ap = argparse.ArgumentParser(description="课表截图 -> timetable_data.json（无 OCR）")
    ap.add_argument("folder")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--week", default="", help="周日期段，如 9.14-9.20")
    ap.add_argument("--week-no", default="", help="第几周，如 3（用于 PDF 文件名）")
    ap.add_argument("--monday", default="")
    ap.add_argument("--title", default="实验室人员空闲表")
    ap.add_argument("--first-day", default="", help="截图首列是星期几，如 周一/周日；"
                    "按文件区分时写 关键词=周X，多个用逗号隔开，如 \"某人=周日\"")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    # ---- 首列星期：默认周一在前
    def norm_day(v):
        """把 App 上的简写（周一/周日）规范成 DAYS 里的全称。"""
        v = v.strip()
        if v in DAYS:
            return v
        if re.fullmatch(r"周[一二三四五六日天]", v):
            return "星期" + ("日" if v[-1] in "日天" else v[-1])
        return None

    rules, default_first = [], "周一"
    for item in str(args.first_day).replace("，", ",").split(","):
        item = item.strip()
        if not item:
            continue
        if "=" in item:
            k, v = item.split("=", 1)
            rules.append((k.strip(), v.strip()))
        else:
            default_first = item
    rules = [(k, norm_day(v)) for k, v in rules]
    default_first = norm_day(default_first)
    bad = [v for _, v in rules if v is None] + ([default_first] if default_first is None else [])
    if bad:
        print(f"[错误] --first-day 里的星期写错了：{'、'.join(str(x) for x in bad)}"
              f"（应为 周一…周日 之一）")
        return 1

    def first_day_of(fn):
        for k, v in rules:
            if k and k in fn:
                return v
        return default_first

    def remap(cells, first_day):
        """列号 -> 真实星期号。首列是周一时保持不变。"""
        f = DAYS.index(first_day)          # 周一=0 … 周日=6
        if f == 0:
            return cells
        return sorted([(f + c - 1) % 7 + 1, s] for c, s in cells)

    allf = [f for f in sorted(os.listdir(args.folder)) if f.lower().endswith(IMG_EXT)]
    files = [f for f in allf if not any(k in f for k in SKIP_FN)]
    skipped = [f for f in allf if f not in files]
    if not files:
        print(f"[错误] {args.folder} 里没有图片")
        return 1

    people, notes = [], []
    print(f"共 {len(files)} 张截图" + (f"（跳过非课表图 {len(skipped)} 张：{'、'.join(skipped)}）"
                                      if skipped else "") + "：")
    for fn in files:
        names = names_from_file(fn)
        busy, lay, _ = busy_cells(os.path.join(args.folder, fn), args.debug)
        fd = first_day_of(fn)
        cells = remap(sorted([list(b) for b in busy]), fd)
        for name in names:
            people.append({"name": name, "busy": [list(x) for x in cells]})
        tail = "" if lay["n_days"] == 7 else f"  ⚠ 只识别到 {lay['n_days']} 列（截图未含周末）"
        if tail:
            notes.append(f"{'、'.join(names)}{tail}")
        extra = f"（同班共用，{len(names)} 人）" if len(names) > 1 else ""
        if fd != "星期一":
            extra += f"  ⚠ 首列={fd}，列号已按「{fd}在前」重排"
            notes.append(f"{'、'.join(names)}：首列={fd}（非周一），已重排星期")
        print(f"  {fn}  ->  {'、'.join(names)}{extra}：{len(busy)} 个有课时段{tail}")

    data = {
        "meta": {"title": args.title, "week_label": args.week,
                 "week_no": args.week_no, "monday_date": args.monday},
        "slots": SLOTS,
        "days": DAYS,
        "people": people,
    }
    out = args.out or os.path.join(args.folder, "timetable_data.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"\n已写出：{out}")
    if notes:
        print("注意：")
        for n in notes:
            print("  - " + n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
