#!/usr/bin/env python3
"""
extra_metrics_table.py -- the noteworthy NON-PPA metrics from the ECE 260C
MP1 evaluator outputs, for all 7 designs.  Companion to ppa_table.py.

Covers exactly the QoR items the MP1 spec lists beyond power/timing/area:
clustering effectiveness (how many flops merged, into what), empty MBFF
pins, clock-network simplification (buffers / sinks), displacement
(legalizability), and the logical-equivalence / rule-compliance checks
(half-connected D-Q pairs, missing combinational cells).

It prints two aligned text tables, and (by default) writes a Markdown file
and a single combined CSV for the report.

Path resolution (first existing wins; same as ppa_table.py):
  clustered : result/<design>_report.json | runs/<design>/report.json
  baseline  : runs/<design>/baseline.json | result/<design>_baseline.json

Examples:
  python3 extra_metrics_table.py
  python3 extra_metrics_table.py --report-dir result --baseline-dir runs
  python3 extra_metrics_table.py --report-glob 'out/{design}.json'
"""

import argparse
import csv
import json
import os
import sys

DESIGNS = ["gcd_v1", "ibex_v1", "ibex_v2", "jpeg_v1", "jpeg_v2",
           "riscv32i_v1", "riscv32i_v2"]


# ---------------------------------------------------------------------------
# File resolution / loading  (identical convention to ppa_table.py)
# ---------------------------------------------------------------------------

def find_first(cands):
    for p in cands:
        if p and os.path.isfile(p):
            return p
    return None


def resolve_paths(design, a):
    if a.report_glob:
        rep = a.report_glob.format(design=design)
        rep = rep if os.path.isfile(rep) else None
    else:
        rep = find_first([
            os.path.join(a.report_dir, f"{design}_report.json"),
            os.path.join(a.report_dir, design, "report.json"),
            os.path.join("runs", design, "report.json"),
            f"{design}_report.json",
        ])
    if a.baseline_glob:
        base = a.baseline_glob.format(design=design)
        base = base if os.path.isfile(base) else None
    else:
        base = find_first([
            os.path.join(a.baseline_dir, design, "baseline.json"),
            os.path.join(a.baseline_dir, f"{design}_baseline.json"),
            os.path.join("result", f"{design}_baseline.json"),
            f"{design}_baseline.json",
            f"baseline_{design}.json",
        ])
    return rep, base


def load(path):
    if not path:
        return None
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError) as e:
        print(f"  ! could not read {path}: {e}", file=sys.stderr)
        return None


def g(d, *keys, default=None):
    cur = d
    for k in keys:
        if isinstance(cur, dict) and k in cur and cur[k] is not None:
            cur = cur[k]
        else:
            return default
    return cur


def numf(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def delta_pct(base, clus):
    if base is None or clus is None:
        return None
    if base == 0:
        return 0.0 if clus == 0 else float("inf")
    return (clus - base) / base * 100.0


def d_str(p):
    if p is None:
        return "n/a"
    if p == float("inf"):
        return "+inf%"
    return f"{p:+.1f}%"


def short_masters(mm):
    """{'sg13g2_dfrbpq_H2V2X_1': 9} -> 'H2V2X_1:9'."""
    if not mm:
        return "-"
    out = []
    for k, v in sorted(mm.items()):
        nm = k.replace("sg13g2_dfrbpq_", "").replace("sg13g2_", "")
        out.append(f"{nm}:{v}")
    return ",".join(out)


# ---------------------------------------------------------------------------
# Per-design derived metrics
# ---------------------------------------------------------------------------

def derive(design, R, B):
    """Return a dict of the noteworthy non-PPA figures for one design."""
    d = {"design": design, "ok_report": R is not None,
         "ok_baseline": B is not None}
    if R is None:
        return d

    orig_ffs = g(B, "total_ffs")
    post_ffs = g(R, "total_ffs")
    single_c = g(R, "single_ff_count")
    mbff_c = g(R, "mbff_count", default=0)

    # "ratio of cells changed into MBFFs": flops absorbed / original flops
    if orig_ffs is not None and single_c is not None:
        merged = orig_ffs - single_c
        merged_pct = (merged / orig_ffs * 100.0) if orig_ffs else 0.0
    else:                                   # no baseline -> fall back
        merged = None
        merged_pct = (float(g(R, "mbff_ratio", default=0)) * 100.0)

    d.update({
        "orig_ffs": orig_ffs,
        "post_ffs": post_ffs,
        "mbff_count": mbff_c,
        "merged": merged,
        "merged_pct": merged_pct,
        "masters": short_masters(g(R, "mbff_masters", default={})),
        "empty_pins": g(R, "empty_pins", default=0),

        "clkbuf_b": g(B, "clock_buffer_count"),
        "clkbuf_c": g(R, "clock_buffer_count"),
        "sinks_b": g(B, "sink_count"),
        "sinks_c": g(R, "sink_count"),

        "disp_tot_b": numf(g(B, "displacement", "total_um")),
        "disp_tot_c": numf(g(R, "displacement", "total_um")),
        "disp_max_c": numf(g(R, "displacement", "max_um")),
        "disp_mean_c": numf(g(R, "displacement", "mean_um")),

        "half": g(R, "half_connected_pin_pairs", default=0) or 0,
        "miss": len(g(R, "missing_comb_cells", default=[]) or []),
        "added": len(g(R, "added_comb_cells", default=[]) or []),
    })
    d["equiv_ok"] = (d["half"] == 0 and d["miss"] == 0)
    return d


# ---------------------------------------------------------------------------
# Text rendering
# ---------------------------------------------------------------------------

def _aligned(headers, rows):
    cols = list(zip(*([headers] + rows))) if rows else [headers]
    w = [max(len(str(c)) for c in col) for col in cols]
    bar = "-+-".join("-" * x for x in w)
    line = lambda r: " | ".join(str(c).ljust(w[i]) for i, c in enumerate(r))
    out = [line(headers), bar]
    out += [line(r) for r in rows]
    return "\n".join(out), bar


def render_text(D):
    s = []
    s.append("=== Clustering effectiveness & clock-network simplification ===")
    s.append("(MBFFs, % of original flops merged, cell-type mix, empty slots; "
             "clock buffers & sinks shown baseline->clustered with delta%, "
             "fewer is better)")
    h1 = ["design", "FFs b->c", "MBFFs", "merged%", "masters",
          "empty", "clkbuf b->c (d%)", "sinks b->c (d%)"]
    r1 = []
    for x in D:
        if not x["ok_report"]:
            r1.append([x["design"], "MISSING report", "", "", "", "", "", ""])
            continue
        ffs = (f"{x['orig_ffs']}->{x['post_ffs']}"
               if x["orig_ffs"] is not None else f"{x['post_ffs']}")
        cb = (f"{x['clkbuf_b']}->{x['clkbuf_c']} "
              f"({d_str(delta_pct(numf(x['clkbuf_b']), numf(x['clkbuf_c'])))})"
              if x["clkbuf_b"] is not None and x["clkbuf_c"] is not None
              else (f"{x['clkbuf_c']}" if x["clkbuf_c"] is not None else "-"))
        sk = (f"{x['sinks_b']}->{x['sinks_c']} "
              f"({d_str(delta_pct(numf(x['sinks_b']), numf(x['sinks_c'])))})"
              if x["sinks_b"] is not None and x["sinks_c"] is not None
              else (f"{x['sinks_c']}" if x["sinks_c"] is not None else "-"))
        r1.append([x["design"], ffs, x["mbff_count"],
                   f"{x['merged_pct']:.1f}%", x["masters"],
                   x["empty_pins"], cb, sk])
    t1, _ = _aligned(h1, r1)
    s.append(t1)
    s.append("")
    s.append("=== Displacement (legalizability) & logical-equivalence ===")
    s.append("(displacement is from the original placement; half-conn D/Q "
             "pairs and missing comb cells MUST be 0 for a valid result)")
    h2 = ["design", "disp_tot um b->c (d%)", "disp_max um",
          "disp_mean um", "half_conn", "miss_comb", "add_comb", "EQUIV"]
    r2 = []
    for x in D:
        if not x["ok_report"]:
            r2.append([x["design"], "MISSING report", "", "", "", "", "", ""])
            continue
        dt = (f"{x['disp_tot_b']:.0f}->{x['disp_tot_c']:.0f} "
              f"({d_str(delta_pct(x['disp_tot_b'], x['disp_tot_c']))})"
              if x["disp_tot_b"] is not None and x["disp_tot_c"] is not None
              else (f"{x['disp_tot_c']:.0f}"
                    if x["disp_tot_c"] is not None else "-"))
        r2.append([
            x["design"], dt,
            "-" if x["disp_max_c"] is None else f"{x['disp_max_c']:.2f}",
            "-" if x["disp_mean_c"] is None else f"{x['disp_mean_c']:.2f}",
            x["half"], x["miss"], x["added"],
            "OK" if x["equiv_ok"] else
            f"FLAG(h{x['half']},m{x['miss']})",
        ])
    t2, _ = _aligned(h2, r2)
    s.append(t2)
    return "\n".join(s)


# ---------------------------------------------------------------------------
# Markdown + CSV
# ---------------------------------------------------------------------------

def render_markdown(D):
    out = ["## Clustering effectiveness & clock network", ""]
    c1 = ["Design", "Orig FFs", "Post FFs", "MBFFs", "Merged %",
          "MBFF masters", "Empty pins", "Clk buf (b->c)", "Sinks (b->c)"]
    out += ["| " + " | ".join(c1) + " |",
            "|" + "|".join("---" for _ in c1) + "|"]
    for x in D:
        if not x["ok_report"]:
            out.append(f"| {x['design']} | " +
                       " | ".join(["_missing_"] * (len(c1) - 1)) + " |")
            continue
        out.append("| " + " | ".join(str(v) for v in [
            x["design"], x["orig_ffs"], x["post_ffs"], x["mbff_count"],
            f"{x['merged_pct']:.1f}%", x["masters"], x["empty_pins"],
            f"{x['clkbuf_b']}->{x['clkbuf_c']}",
            f"{x['sinks_b']}->{x['sinks_c']}",
        ]) + " |")
    out += ["", "## Displacement & logical equivalence", ""]
    c2 = ["Design", "Disp total um (b->c)", "Disp max um", "Disp mean um",
          "Half-conn D/Q", "Missing comb", "Added comb", "Equivalence"]
    out += ["| " + " | ".join(c2) + " |",
            "|" + "|".join("---" for _ in c2) + "|"]
    for x in D:
        if not x["ok_report"]:
            out.append(f"| {x['design']} | " +
                       " | ".join(["_missing_"] * (len(c2) - 1)) + " |")
            continue
        out.append("| " + " | ".join(str(v) for v in [
            x["design"],
            (f"{x['disp_tot_b']:.0f}->{x['disp_tot_c']:.0f}"
             if x["disp_tot_b"] is not None else f"{x['disp_tot_c']}"),
            x["disp_max_c"], x["disp_mean_c"], x["half"], x["miss"],
            x["added"], "OK" if x["equiv_ok"] else "**FLAG**",
        ]) + " |")
    return "\n".join(out)


def render_csv(D, path):
    cols = ["design", "orig_ffs", "post_ffs", "mbff_count", "merged",
            "merged_pct", "mbff_masters", "empty_pins",
            "clkbuf_baseline", "clkbuf_clustered",
            "sinks_baseline", "sinks_clustered",
            "disp_total_baseline", "disp_total_clustered",
            "disp_max", "disp_mean",
            "half_connected_pin_pairs", "missing_comb_cells",
            "added_comb_cells", "equivalence_ok"]
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for x in D:
            if not x["ok_report"]:
                w.writerow([x["design"]] + [""] * (len(cols) - 1))
                continue
            w.writerow([
                x["design"], x["orig_ffs"], x["post_ffs"], x["mbff_count"],
                x["merged"], round(x["merged_pct"], 2), x["masters"],
                x["empty_pins"], x["clkbuf_b"], x["clkbuf_c"],
                x["sinks_b"], x["sinks_c"], x["disp_tot_b"], x["disp_tot_c"],
                x["disp_max_c"], x["disp_mean_c"], x["half"], x["miss"],
                x["added"], x["equiv_ok"],
            ])


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="MP1 non-PPA QoR table (clustering / clock / displacement "
                    "/ equivalence) for the 7 designs")
    ap.add_argument("--designs", nargs="+", default=DESIGNS)
    ap.add_argument("--report-dir", default="result")
    ap.add_argument("--baseline-dir", default="runs")
    ap.add_argument("--report-glob", default=None,
                    help="template, e.g. 'out/{design}.json'")
    ap.add_argument("--baseline-glob", default=None,
                    help="template, e.g. 'base/{design}.json'")
    ap.add_argument("--md", default="extra_metrics.md")
    ap.add_argument("--csv", default="extra_metrics.csv")
    ap.add_argument("--no-files", action="store_true")
    a = ap.parse_args()

    D = []
    for d in a.designs:
        rp, bp = resolve_paths(d, a)
        D.append(derive(d, load(rp), load(bp)))

    print(render_text(D))
    print()

    miss = [x["design"] for x in D if not x["ok_report"]]
    if miss:
        print(f"note: no clustered report for: {', '.join(miss)}")
    flags = [x["design"] for x in D
             if x["ok_report"] and not x["equiv_ok"]]
    if flags:
        print(f"!! EQUIVALENCE FLAG (result not valid as-is) for: "
              f"{', '.join(flags)}")
    found = sum(1 for x in D if x["ok_report"])
    print(f"resolved {found}/{len(D)} clustered reports.")

    if not a.no_files:
        if a.md:
            with open(a.md, "w") as fh:
                fh.write("# MP1 non-PPA QoR summary\n\n")
                fh.write(render_markdown(D) + "\n\n")
                fh.write("_Half-connected D/Q pairs and missing combinational "
                         "cells must be 0 for a valid submission; added "
                         "combinational cells are expected repair buffers._\n")
            print(f"wrote {a.md}")
        if a.csv:
            render_csv(D, a.csv)
            print(f"wrote {a.csv}")


if __name__ == "__main__":
    main()