#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""实验分组 × 空闲表 → 并行排期建议（含工作位 / 工位约束）

把「谁什么时候有空」「谁和谁一组」「哪组要哪个工作位」对起来，算出：
  1. 跨组人员 —— 同一时段不能同时在两组（硬约束）
  2. 每组能「全员到齐」的时段（组内所有人空闲的交集）
  3. 最少需要几个时段才能把全部组排完（同一时段可并行多组）
  4. 一份建议排期（PNG + TXT），并给出等价的备选方案

硬约束（同一时段内）：
  - 组内成员不能撞人（跨组同学同一时段只能在一个组）
  - 同一个工作位不能被两组同时占用；不需要工作位的组不受此限
  - 同一时段并行的组数 ≤ 实验室工位数

评分（越小越好）：
  ① 占用的时段数  ② 同一人同一天跑两组的次数  ③ 用了几个晚上时段
  ④ 占用了几天  ⑤ 时段序号（稳定排序）

用法:
  python schedule_experiments.py timetable_data.json --groups groups.json -o out
  python schedule_experiments.py ... --hoods hoods.json          # 加工作位约束
  python schedule_experiments.py ... --no-night                  # 不排晚上
  python schedule_experiments.py ... --slots 1,2,3,4             # 只用指定大节

hoods.json 格式:
  {"hoods": ["工作位1", "工作位2", "工作位3"],
   "map":   {"组A": "工作位1", "组B": "工作位2",
             "组C": "工作位3", "组D": null, "组E": null}}
  map：组名 → 工作位名（化学实验室里就是通风橱）；不需要的填 null 或直接省略。

groups.json 格式（组内第一个名字视为组长）:
  {"组A": ["甲一", "乙二", "丙三"], "组B": ["丁四", "戊五"], ...}
"""
import argparse
import itertools
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from render_freetime import (  # noqa: E402
    DEFAULT_DAYS, DEFAULT_SLOTS, GREEN_HEAD, GREEN_TEXT, LINE,
    _cjk_plt, week_dates, wrap_names,
)

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
MAX_SOLUTIONS = 300000


# ---------------------------------------------------------------- 工具
def short_slot(slot):
    return slot.replace(" ", "")


def is_night(slot):
    return ("晚" in slot) or ("夜" in slot)


def hood_short(name):
    for k in ("通风橱", "工位", "工作位"):
        name = (name or "").replace(k, "")
    return name or "无"


# ---------------------------------------------------------------- 读取输入
def load_inputs(data_path, groups_path, ndays):
    with open(data_path, "r", encoding="utf-8-sig") as f:
        raw = json.load(f)
    with open(groups_path, "r", encoding="utf-8-sig") as f:
        graw = json.load(f)

    slots = raw.get("slots") or DEFAULT_SLOTS
    days = raw.get("days") or DEFAULT_DAYS
    people = raw.get("people") or []
    if ndays and ndays < len(days):
        days = days[:ndays]

    busy = {}
    for p in people:
        name = (p.get("name") or "").strip()
        if not name:
            continue
        s = set()
        for it in p.get("busy") or []:
            try:
                d, sl = int(it[0]), int(it[1])
            except (TypeError, ValueError, IndexError):
                continue
            if 1 <= d <= len(days) and 1 <= sl <= len(slots):
                s.add((d, sl))
        busy[name] = s

    src = graw.get("groups") if (isinstance(graw, dict) and isinstance(graw.get("groups"), dict)) else graw
    groups = []
    for gname, members in src.items():
        if str(gname).startswith("_") or not isinstance(members, (list, tuple)):
            continue          # 跳过 "_说明" 这类注释键与非列表值
        mem = [str(x).strip() for x in members if str(x).strip()]
        if mem:
            groups.append((str(gname), mem))

    return raw.get("meta", {}) or {}, slots, days, busy, groups


def load_hoods(path, group_names):
    """→ (可用工作位列表, {组名: 工作位名 or None})"""
    hoods, hood_of = [], {g: None for g in group_names}
    if not path:
        return hoods, hood_of
    with open(path, "r", encoding="utf-8-sig") as f:
        raw = json.load(f)
    hoods = [str(h).strip() for h in (raw.get("hoods") or []) if str(h).strip()]
    mp = raw.get("map") or raw.get("groups") or {}
    for k, v in mp.items():
        if str(k).startswith("_"):
            continue          # 跳过注释键
        if k in hood_of:
            v = v.strip() if isinstance(v, str) else v
            hood_of[k] = v or None
    for h in list(hood_of.values()):
        if h and h not in hoods:
            hoods.append(h)
    return hoods, hood_of


# ---------------------------------------------------------------- 组合分析
def _subsets(n, cap=18):
    if n > cap:
        raise SystemExit(f"[!] 分组数 {n} 太多，组合分析已跳过")
    for mask in range(1, 1 << n):
        yield [i for i in range(n) if mask >> i & 1]


def max_clique(memsets, hood_of, groups, n):
    """两两不能同时开的组：共享成员，或抢同一个工作位 → 它们必须占不同时段。
    最大团的大小 = 时段数的下界。"""
    best = []
    for idx in _subsets(n):
        if len(idx) <= len(best):
            continue
        ok = True
        for a in range(len(idx)):
            for b in range(a + 1, len(idx)):
                i, j = idx[a], idx[b]
                hi, hj = hood_of[groups[i][0]], hood_of[groups[j][0]]
                if not (memsets[i] & memsets[j]) and not (hi and hj and hi == hj):
                    ok = False
                    break
            if not ok:
                break
        if ok:
            best = idx
    return best


def max_disjoint(memsets, hood_of, groups, n):
    """可以同时开（互不撞人、不抢同一工作位）的最大组集合 = 并行度上限。"""
    best = []
    for idx in _subsets(n):
        if len(idx) <= len(best):
            continue
        ok = True
        for a in range(len(idx)):
            for b in range(a + 1, len(idx)):
                i, j = idx[a], idx[b]
                hi, hj = hood_of[groups[i][0]], hood_of[groups[j][0]]
                if (memsets[i] & memsets[j]) or (hi and hj and hi == hj):
                    ok = False
                    break
            if not ok:
                break
        if ok:
            best = idx
    return best


# ---------------------------------------------------------------- 求解
def solve(groups, cand, hood_of, cap=MAX_SOLUTIONS):
    n = len(groups)
    memsets = [set(m) for _g, m in groups]
    order = sorted(range(n), key=lambda i: (len(cand[i]), -len(memsets[i]), i))
    sols, assign = [], {}
    slot_state = defaultdict(list)

    def ok(gi, slot):
        hi = hood_of[groups[gi][0]]
        for gj in slot_state[slot]:
            if memsets[gi] & memsets[gj]:
                return False
            hj = hood_of[groups[gj][0]]
            if hi and hj and hi == hj:
                return False
        return True

    def rec(k):
        if len(sols) >= cap:
            return
        if k == n:
            sols.append(dict(assign))
            return
        gi = order[k]
        for slot in cand[gi]:
            if ok(gi, slot):
                slot_state[slot].append(gi)
                assign[gi] = slot
                rec(k + 1)
                del assign[gi]
                slot_state[slot].pop()

    rec(0)
    return sols


def cost_of(sol, groups, night_slots):
    slots = set(sol.values())
    seen = defaultdict(int)
    per_slot = defaultdict(int)
    for gi, (d, sl) in sol.items():
        per_slot[(d, sl)] += 1
        for m in groups[gi][1]:
            seen[(m, d)] += 1
    dup = sum(v - 1 for v in seen.values() if v > 1)
    nnight = sum(1 for (_d, sl) in slots if sl in night_slots)
    ndays_used = len({d for (d, _sl) in slots})
    crowd = max(per_slot.values()) if per_slot else 0
    # ① 时段数最少 ② 无人同一天跑两组 ③ 同一时段别挤太多组 ④ 少用晚上 ⑤ 占用的天数 ⑥ 稳定排序
    return (len(slots), dup, crowd, nnight, ndays_used, sorted(slots))


# ------------------------------------------------- 组合式求解（候选多时用这个）
def _feasible_at(memsets, hood_of, groups, gi, gj):
    if memsets[gi] & memsets[gj]:
        return False
    hi, hj = hood_of[groups[gi][0]], hood_of[groups[gj][0]]
    return not (hi and hj and hi == hj)


def assign_within(sub, groups, hood_of, cap=800):
    """在给定的「每组候选时段子集」内枚举全部可行分配（子集小，很快）"""
    n = len(groups)
    memsets = [set(m) for _g, m in groups]
    order = sorted(range(n), key=lambda i: (len(sub[i]), -len(memsets[i]), i))
    out, assign = [], {}
    slot_state = defaultdict(list)

    def rec(k):
        if len(out) >= cap:
            return
        if k == n:
            out.append(dict(assign))
            return
        gi = order[k]
        for slot in sub[gi]:
            if all(_feasible_at(memsets, hood_of, groups, gi, gj) for gj in slot_state[slot]):
                slot_state[slot].append(gi)
                assign[gi] = slot
                rec(k + 1)
                del assign[gi]
                slot_state[slot].pop()

    rec(0)
    return out


def best_plan(groups, cand, hood_of, night_slots, kmin=1, kmax=8):
    """先枚举「用哪 k 个时段」的集合，再在集合内分配 —— k 从小往大，取到即最优。

    比直接枚举 5 个组的全部组合稳得多（候选时段一多，后者会指数爆炸）。
    返回 (最优分配 dict, 打分 tuple) 或 (None, None)。
    """
    pool = sorted({s for lst in cand.values() for s in lst})
    if not pool:
        return None, None
    for k in range(max(1, kmin), min(len(pool), kmax) + 1):
        best = None
        for S in itertools.combinations(pool, k):
            Ss = set(S)
            sub = {gi: [s for s in cand[gi] if s in Ss] for gi in cand}
            if any(not sub[gi] for gi in cand):
                continue
            for sol in assign_within(sub, groups, hood_of):
                c = cost_of(sol, groups, night_slots)
                if best is None or c < best[0]:
                    best = (c, dict(sol))
        if best:
            return best[1], best[0]
    return None, None


def solutions_in_slots(groups, cand, hood_of, slots_used, cap=2000):
    """固定时段集合，枚举其中所有等价分配（用来列备选方案）"""
    Ss = set(slots_used)
    sub = {gi: [s for s in cand[gi] if s in Ss] for gi in cand}
    return assign_within(sub, groups, hood_of, cap=cap)


def describe(sol, groups, days, slots, hood_of):
    """→ [(时段文本, 组名, 成员文本, 工作位文本)]，按时段排序"""
    by_slot = defaultdict(list)
    for gi, (d, sl) in sol.items():
        by_slot[(d, sl)].append(gi)
    rows = []
    for (d, sl) in sorted(by_slot):
        for gi in sorted(by_slot[(d, sl)]):
            gname, mem = groups[gi]
            lead = mem[0] if mem else ""
            shown = "、".join((m + "*" if m == lead else m) for m in mem)
            h = hood_of.get(gname)
            rows.append((f"{days[d - 1]} {short_slot(slots[sl - 1])}", gname, shown, h or "无需工作位"))
    return rows


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser(description="实验分组 × 空闲表 -> 并行排期建议")
    ap.add_argument("data", help="timetable_data.json")
    ap.add_argument("--groups", required=True, help="分组 JSON")
    ap.add_argument("--hoods", default=None, help="工作位 JSON（可选；化学实验室里填通风橱）")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--name", default="实验排期建议")
    ap.add_argument("--days", type=int, default=0, help="只考虑前 N 天（常用 5）")
    ap.add_argument("--slots", default=None, help="只用这些大节编号，如 1,2,3,4")
    ap.add_argument("--day-set", default=None, help="只用这几天的编号，如 1,2,3,4,5 或 7（周日=7）")
    ap.add_argument("--no-night", action="store_true", help="不排晚上")
    ap.add_argument("--stations", type=int, default=0,
                    help="实验室工位数（通风橱/工作位数量）。0 = 按 hoods.json 推断")
    ap.add_argument("--alternatives", type=int, default=4, help="列出几个等价的备选方案")
    args = ap.parse_args()

    meta, slots, days, busy, groups = load_inputs(args.data, args.groups, args.days)
    nd, ns = len(days), len(slots)
    hoods, hood_of = load_hoods(args.hoods, [g for g, _m in groups])
    nstations = args.stations or len(hoods) or 0

    # 可用日子
    if args.day_set:
        use_days = [int(x) for x in args.day_set.replace("，", ",").split(",") if x.strip().isdigit()]
        use_days = [d for d in use_days if 1 <= d <= nd]
    else:
        use_days = list(range(1, nd + 1))
    if not use_days:
        use_days = list(range(1, nd + 1))

    # 可用大节
    if args.slots:
        keep = [int(x) for x in args.slots.replace("，", ",").split(",") if x.strip().isdigit()]
        keep = [k for k in keep if 1 <= k <= ns]
    else:
        keep = list(range(1, ns + 1))
    if args.no_night:
        keep = [k for k in keep if not is_night(slots[k - 1])]
    if not keep:
        keep = list(range(1, ns + 1))
    night_slots = {k for k in keep if is_night(slots[k - 1])}

    free = {}
    for name, s in busy.items():
        free[name] = {(d, sl) for d in use_days for sl in keep if (d, sl) not in s}

    out = []
    out.append("=" * 64)
    out.append(f"实验排期建议（并行）　{meta.get('week_label', '')}".rstrip())
    out.append("=" * 64)
    if nstations:
        out.append(f"实验室工位：{nstations} 个" + ("　（" + "、".join(hoods) + "）" if hoods else ""))
    out.append(f"可用时段：{'、'.join(days[d - 1] for d in use_days)}"
               f" × {len(keep)} 个大节"
               + ("　（已排除晚上）" if args.no_night else ""))

    missing = sorted({m for _g, mem in groups for m in mem if m not in busy})
    if missing:
        out.append("")
        out.append(f"[!] 分组里有 {len(missing)} 人不在课表名单中，已跳过：{'、'.join(missing)}")

    memsets = [set(m) for _g, m in groups]
    n = len(groups)

    # ---- 一、跨组人员
    belong = defaultdict(list)
    for gname, mem in groups:
        for m in mem:
            belong[m].append(gname)
    cross = {m: g for m, g in belong.items() if len(g) > 1}
    out.append("")
    out.append("【一】跨组人员 —— 同一时段不能同时在两组，这是最大的约束")
    if cross:
        for m, g in sorted(cross.items(), key=lambda kv: -len(kv[1])):
            out.append(f"  {m}　{len(g)} 个组：{'、'.join(g)}")
    else:
        out.append("  无（没有人跨组，各组可完全独立排期）")

    # ---- 二、各组可全员到齐的时段
    out.append("")
    out.append("【二】各组「全员到齐」的可用时段")
    cand = {}
    for gi, (gname, mem) in enumerate(groups):
        known = [m for m in mem if m in free]
        inter = set(free[known[0]]) if known else set()
        for m in known[1:]:
            inter &= free[m]
        cand[gi] = sorted(inter, key=lambda x: (x[0], x[1]))
        lead = mem[0] if mem else ""
        shown = "、".join((m + "*" if m == lead else m) for m in mem)
        h = hood_of.get(gname)
        tag = f"　[{h}]" if h else "　[无需工作位]"
        out.append("")
        out.append(f"  {gname}（{shown}）{tag}")
        if not cand[gi]:
            out.append("    [!] 没有任何时段能全员到齐 —— 需要换人或降级为「不全员到场」")
            continue
        out.append(f"    共 {len(cand[gi])} 个："
                   + "、".join(f"{days[d - 1]} {short_slot(slots[sl - 1])}" for d, sl in cand[gi]))
        if len(cand[gi]) == 1:
            out.append("    [!] 只有 1 个时段，没有腾挪空间")
        knownn = sorted(((len(free[m]), m) for m in known))
        if len(knownn) > 1 and knownn[0][0] <= len(cand[gi]) + 2:
            out.append(f"    瓶颈：{knownn[0][1]} 整周只有 {knownn[0][0]} 个时段空闲")

    # ---- 三、并行度
    out.append("")
    out.append("【三】并行度：同一时段最多能开几组")
    if n:
        clq = max_clique(memsets, hood_of, groups, n)
        dis = max_disjoint(memsets, hood_of, groups, n)
        out.append(f"  结构上最多可同时开 {len(dis)} 组："
                   + " ＋ ".join(groups[i][0] for i in dis))
        out.append(f"  但有 {len(clq)} 个组两两冲突，必须各占一个时段："
                   + " ＋ ".join(groups[i][0] for i in clq))
        out.append(f"  → 全部 {n} 组排完，至少需要 {len(clq)} 个时段")
        if nstations:
            if nstations >= len(dis):
                out.append(f"  工位（{nstations} 个）够用：并行上限由人决定，不由工作位决定。")
            else:
                out.append(f"  [!] 工位只有 {nstations} 个，小于并行上限 {len(dis)}，会限制并行。")

    # ---- 四、求解
    out.append("")
    out.append("【四】建议排期")
    best, bc, sols = None, None, []
    if all(cand[i] for i in range(n)):
        best, bc = best_plan(groups, cand, hood_of, night_slots,
                             kmin=len(clq) if n else 1)
        if best:
            sols = solutions_in_slots(groups, cand, hood_of, set(best.values()))

    if best is None:
        out.append("  [!] 找不到让所有组都满足约束的方案。")
        stuck = [groups[i][0] for i in range(n) if not cand[i]]
        if stuck:
            out.append(f"      直接原因：{'、'.join(stuck)} 没有可用时段。")
        out.append("      建议：放宽到「不全员到场」，或换人。")
    else:
        out.append(f"  占用 {bc[0]} 个时段即可排完全部 {n} 组"
                   + (f"（其中同一人同一天跑两组 {bc[1]} 次）" if bc[1] else "（无人同一天跑两组）"))
        out.append(f"  最挤的时段同时开 {bc[2]} 组"
                   + ("　← 已尽量避免多组同时开" if bc[2] < len(groups) else ""))
        out.append(f"  （时段数已是理论下限 {len(clq) if n else 0}；同一时段最多挤 3 组也能做到，"
                   "但那意味着 7 个人一起做，不推荐）")
        out.append("")
        rows = describe(best, groups, days, slots, hood_of)
        w1 = max(len(r[0]) for r in rows)
        w2 = max(len(r[1]) for r in rows)
        w3 = max(len(r[2]) for r in rows)
        for slot_txt, gname, shown, htxt in rows:
            out.append(f"    {slot_txt:<{w1}}　{gname:<{w2}}　{shown:<{w3}}　{htxt}")
        # 每时段并行的组数
        per = defaultdict(int)
        for gi, (d, sl) in best.items():
            per[(d, sl)] += 1
        out.append("")
        for (d, sl) in sorted(per):
            nms = [groups[gi][0] for gi, t in best.items() if t == (d, sl)]
            hh = [hood_short(hood_of.get(x)) for x in nms if hood_of.get(x)]
            out.append(f"    {days[d - 1]} {short_slot(slots[sl - 1])}：并行 {per[(d, sl)]} 组"
                       f"（{'、'.join(nms)}）"
                       f"　工作位占用 {len(hh)}/{nstations or len(hh)}"
                       + (f"：{'、'.join(hh)}" if hh else "（都不占工作位）"))

        # 等价方案
        out.append("")
        out.append(f"  等价方案共 {len(sols)} 个（时段数与人次完全相同的写法）：")
        shown_alt = 0
        uniq = set()
        for s in sols:
            key = tuple(describe(s, groups, days, slots, hood_of))
            if key in uniq:
                continue
            uniq.add(key)
            if shown_alt >= args.alternatives - 1:
                continue
            shown_alt += 1
            out.append("    · " + " ｜ ".join(
                f"{g}@{t}" for t, g, _s, _h in describe(s, groups, days, slots, hood_of)))
        if shown_alt == 0:
            out.append("    （无其他等价写法）")

    # ---- 五、需要确认的
    out.append("")
    out.append("【五】需要你确认的")
    out.append("  1. 按「一个大节做完一轮实验」估的。若一轮要跨 2 个连续大节，"
               "需改用「连续两格都空闲」重算。")
    out.append("  2. 同一时段并行的前提是：实验室的仪器/台面够用、且你有精力同时盯几个组。")
    out.append("  3. 一个班（课表相同）的同学若有人没交截图，直接复用同班同学的有课数据。")

    report = "\n".join(out)
    print(report)

    outdir = args.out or os.path.dirname(os.path.abspath(args.data))
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, args.name + ".txt"), "w", encoding="utf-8") as f:
        f.write(report + "\n")

    if best is not None:
        try:
            render_schedule_png(meta, slots, days, groups, best,
                                week_dates(meta.get("monday_date", ""), days),
                                os.path.join(outdir, args.name + ".png"),
                                hood_of=hood_of, hoods=hoods, nstations=nstations)
            print("已生成：")
            print("  " + os.path.join(outdir, args.name + ".png"))
            print("  " + os.path.join(outdir, args.name + ".txt"))
        except Exception as e:
            import traceback
            print(f"[warn] 排期图渲染失败：{e.__class__.__name__}: {e}")
            traceback.print_exc()
    return 0


# ---------------------------------------------------------------- 画图
def render_schedule_png(meta, slots, days, groups, assign, dates, path,
                        hood_of=None, hoods=None, nstations=0):
    plt = _cjk_plt()
    from matplotlib.patches import Rectangle

    hood_of = hood_of or {}
    hoods = hoods or []
    ns, nd = len(slots), len(days)

    grid = defaultdict(lambda: defaultdict(list))
    for gi, (d, sl) in assign.items():
        grid[sl][d].append(gi)
    rows = sorted(grid.keys())
    if not rows:
        return

    label_w, col_w = 1.46, 2.24
    base_h = 1.20
    hdr_h, title_h = 0.94, 0.74
    x0 = 0.18
    row_hs = {}
    for sl in rows:
        k = max(max((len(grid[sl][d]) for d in range(1, nd + 1)), default=0), 1)
        row_hs[sl] = base_h * k + 0.12
    W = x0 * 2 + label_w + col_w * nd
    H = title_h + hdr_h + sum(row_hs.values()) + 0.52
    fig = plt.figure(figsize=(W, H), dpi=200)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W)
    ax.set_ylim(0, H)
    ax.invert_yaxis()
    ax.axis("off")

    def txt(x, y, s, fs, color="#1f2328", bold=False, ha="center"):
        ax.text(x, y, s, fontsize=fs, va="center", ha=ha, color=color,
                weight="bold" if bold else "normal")

    txt(x0, title_h * 0.46, f"实验排期建议　{meta.get('week_label', '')}".rstrip(),
        13, bold=True, ha="left")
    sub = "同一时段可并行多组：跨组同学不撞车、工作位不抢同一个"
    txt(W - x0, title_h * 0.46, sub, 8.5, "#6b7280", ha="right")
    if nstations:
        line2 = f"实验室 {nstations} 个工作位"
        if hoods:
            line2 += "：" + "、".join(hood_short(h) for h in hoods)
        txt(x0, title_h * 0.84, line2, 8.5, "#4b7a56", ha="left")

    y = title_h
    ax.add_patch(Rectangle((x0, y), label_w, hdr_h, facecolor="#" + GREEN_HEAD, edgecolor="white"))
    txt(x0 + label_w / 2, y + hdr_h / 2, "时段", 10, "#" + GREEN_TEXT, True)
    for di, d in enumerate(days):
        cx = x0 + label_w + di * col_w
        ax.add_patch(Rectangle((cx, y), col_w, hdr_h, facecolor="#" + GREEN_HEAD, edgecolor="white"))
        txt(cx + col_w / 2, y + hdr_h * 0.36, d, 10.5, "#" + GREEN_TEXT, True)
        if dates[di]:
            txt(cx + col_w / 2, y + hdr_h * 0.74, dates[di], 8.5, "#4b7a56")
    y += hdr_h

    for sl in rows:
        row_h = row_hs[sl]
        ax.add_patch(Rectangle((x0, y), label_w, row_h, facecolor="#" + GREEN_HEAD, edgecolor="white"))
        txt(x0 + label_w / 2, y + row_h / 2, short_slot(slots[sl - 1]), 9.5, "#" + GREEN_TEXT, True)
        for di in range(nd):
            d = di + 1
            cx = x0 + label_w + di * col_w
            gs = grid[sl].get(d) or []
            if not gs:
                ax.add_patch(Rectangle((cx + 0.02, y + 0.02), col_w - 0.04, row_h - 0.04,
                                       facecolor="#F7F9F8", edgecolor="#" + LINE, linewidth=0.6))
                continue
            bh = (row_h - 0.04) / len(gs)
            for bi, gi in enumerate(sorted(gs)):
                by = y + 0.02 + bi * bh
                gname, mem = groups[gi]
                bg, fg = GROUP_COLORS[gi % len(GROUP_COLORS)]
                ax.add_patch(Rectangle((cx + 0.02, by), col_w - 0.04, bh,
                                       facecolor=bg, edgecolor=fg, linewidth=1.3))
                txt(cx + col_w / 2, by + 0.19, gname, 10.5, fg, True)
                lead = mem[0] if mem else ""
                shown = "、".join((m + "*" if m == lead else m) for m in mem)
                for li, ln in enumerate(wrap_names([shown], limit=12)):
                    txt(cx + col_w / 2, by + 0.40 + li * 0.185, ln, 7.5, "#3d444c")
                h = hood_of.get(gname)
                txt(cx + col_w / 2, by + bh - 0.14,
                    ("工作位：" + hood_short(h)) if h else "无需工作位", 7, "#6b7280")
        y += row_h

    ly = y + 0.30
    ax.add_patch(Rectangle((x0, ly - 0.065), 0.20, 0.13, facecolor="#F7F9F8",
                           edgecolor="#" + LINE, linewidth=0.6))
    txt(x0 + 0.26, ly, "未安排", 8, "#8a929c", ha="left")
    txt(x0 + 1.40, ly, "姓名后带 * 为小组长", 8, "#8a929c", ha="left")
    txt(x0 + 3.60, ly, "同一格内多组 = 并行进行（各占不同工作位）", 8, "#8a929c", ha="left")

    fig.savefig(path, dpi=200, facecolor="white", bbox_inches="tight", pad_inches=0.14)
    plt.close(fig)


if __name__ == "__main__":
    sys.exit(main())
