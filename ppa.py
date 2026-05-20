#!/usr/bin/env python3
"""
ppa_table.py -- build a Power / Performance / Area comparison table from the
ECE 260C MP1 evaluator outputs (clustered `<design>_report.json` vs
`baseline.json`) for all 7 designs.

It prints an aligned text table to stdout and (by default) also writes a
Markdown table and a CSV you can drop straight into the report.

Path resolution (first existing wins; override with the flags below):
  clustered : result/<design>_report.json
              runs/<design>/report.json
              <design>_report.json
  baseline  : runs/<design>/baseline.json
              result/<design>_baseline.json
              <design>_baseline.json
              baseline_<design>.json

Examples:
  python3 ppa_table.py
  python3 ppa_table.py --report-dir result --baseline-dir runs
  python3 ppa_table.py --designs gcd_v1 jpeg_v1 --no-files
  python3 ppa_table.py --report-glob 'out/{design}.json' \
                       --baseline-glob 'base/{design}.json'
"""

import argparse
import csv
import json
import os
import sys

DESIGNS = ["gcd_v1", "ibex_v1", "ibex_v2", "jpeg_v1", "jpeg_v2",
           "riscv32i_v1", "riscv32i_v2"]

# (key in json, column label, unit-scale, decimals, lower_is_better)
#  TNS is scored on |value| (closer to 0 == better); everything else raw.
METRICS = [
    ("total_power",          "DynPwr (mW)",  1e3, 4, True),
    ("static_power",         "StatPwr (uW)", 1e6, 3, True),
    ("total_negative_slack", "TNS (ns)",     1.0, 3, True),
    ("expected_clock_period","ClkPer (ns)",  1.0, 3, True),
    ("total_area",           "Area (um^2)",  1.0, 1, True),
    ("instance_count",       "Insts",        1.0, 0, True),
    ("total_wirelength",     "WL (um)",      1.0, 2, True),
]


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


def num(d, key):
    if not d:
        return None
    v = d.get(key)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def pct_delta(base, clus, lower_is_better, tns=False):
    """Signed % change of clustered vs baseline. For metrics where lower is
    better a NEGATIVE % means improvement. TNS uses magnitude."""
    if base is None or clus is None:
        return None
    b, c = (abs(base), abs(clus)) if tns else (base, clus)
    if b == 0:
        return 0.0 if c == 0 else float("inf")
    return (c - b) / b * 100.0


def fmt_val(v, scale, dec):
    if v is None:
        return "-"
    v = v * scale
    return f"{v:.{dec}f}" if dec else f"{int(round(v))}"


def fmt_delta(p):
    if p is None:
        return "  n/a"
    if p == float("inf"):
        return " +inf%"
    s = f"{p:+.1f}%"
    return s


def cell(base, clus, scale, dec, lib, tns):
    """'<clustered> (<delta%>)' -- delta sign: negative == better."""
    p = pct_delta(base, clus, lib, tns)
    return f"{fmt_val(clus, scale, dec)} ({fmt_delta(p)})"


def collect(a):
    rows = []
    for d in a.designs:
        rep_p, base_p = resolve_paths(d, a)
        R, B = load(rep_p), load(base_p)
        rows.append({
            "design": d,
            "report_path": rep_p, "baseline_path": base_p,
            "R": R, "B": B,
        })
    return rows


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

def render_text(rows):
    headers = ["design"] + [m[1] for m in METRICS] + ["MBFFs", "eqOK"]
    table = []
    deltas = {m[0]: [] for m in METRICS}
    for r in rows:
        R, B = r["R"], r["B"]
        line = [r["design"]]
        if R is None:
            line += ["MISSING report"] + [""] * (len(METRICS) + 1)
            table.append(line)
            continue
        for key, _lab, sc, dec, lib in [(m[0], m[1], m[2], m[3], m[4])
                                        for m in METRICS]:
            tns = (key == "total_negative_slack")
            b, c = num(B, key), num(R, key)
            p = pct_delta(b, c, lib, tns)
            if p is not None and p != float("inf"):
                deltas[key].append(p)
            line.append(cell(b, c, sc, dec, lib, tns))
        mbff = R.get("mbff_count")
        ratio = R.get("mbff_ratio")
        line.append(f"{mbff} ({float(ratio)*100:.0f}%)"
                    if mbff is not None and ratio is not None else "-")
        half = R.get("half_connected_pin_pairs", 0) or 0
        miss = len(R.get("missing_comb_cells", []) or [])
        line.append("yes" if (half == 0 and miss == 0) else f"NO(h{half},m{miss})")
        table.append(line)

    # average-Δ% footer row
    avg = ["AVG d%"]
    for m in METRICS:
        vals = deltas[m[0]]
        avg.append(f"{(sum(vals)/len(vals)):+.1f}%" if vals else "n/a")
    avg += ["", ""]
    table.append(avg)

    widths = [max(len(str(x)) for x in [h] + [row[i] for row in table])
              for i, h in enumerate(headers)]
    bar = "-+-".join("-" * w for w in widths)

    def emit(cols):
        return " | ".join(str(c).ljust(widths[i])
                          for i, c in enumerate(cols))

    out = []
    out.append("PPA: clustered value (delta% vs baseline). "
               "Lower is better for every metric; TNS uses |value|. "
               "Negative delta% == improvement.")
    out.append("")
    out.append(emit(headers))
    out.append(bar)
    for row in table[:-1]:
        out.append(emit(row + [""] * (len(headers) - len(row))))
    out.append(bar)
    out.append(emit(table[-1] + [""] * (len(headers) - len(table[-1]))))
    return "\n".join(out)


def render_markdown(rows):
    cols = (["Design"]
            + [m[1] for m in METRICS]
            + ["MBFFs", "MBFF%", "Equiv OK"])
    out = ["| " + " | ".join(cols) + " |",
           "|" + "|".join("---" for _ in cols) + "|"]
    for r in rows:
        R, B = r["R"], r["B"]
        if R is None:
            out.append(f"| {r['design']} | " +
                       " | ".join(["_missing_"] * (len(cols) - 1)) + " |")
            continue
        c = [r["design"]]
        for key, _lab, sc, dec, lib in [(m[0], m[1], m[2], m[3], m[4])
                                        for m in METRICS]:
            tns = (key == "total_negative_slack")
            b, v = num(B, key), num(R, key)
            p = pct_delta(b, v, lib, tns)
            c.append(f"{fmt_val(v, sc, dec)} ({fmt_delta(p).strip()})")
        mb = R.get("mbff_count")
        rt = R.get("mbff_ratio")
        c.append("-" if mb is None else str(mb))
        c.append("-" if rt is None else f"{float(rt)*100:.1f}%")
        half = R.get("half_connected_pin_pairs", 0) or 0
        miss = len(R.get("missing_comb_cells", []) or [])
        c.append("yes" if (half == 0 and miss == 0)
                 else f"NO (half={half}, missing={miss})")
        out.append("| " + " | ".join(str(x) for x in c) + " |")
    return "\n".join(out)


def render_csv(rows, path):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        head = ["design"]
        for m in METRICS:
            head += [f"{m[0]}_baseline", f"{m[0]}_clustered", f"{m[0]}_delta_pct"]
        head += ["mbff_count", "mbff_ratio", "half_connected_pin_pairs",
                 "missing_comb_cells", "report_path", "baseline_path"]
        w.writerow(head)
        for r in rows:
            R, B = r["R"], r["B"]
            row = [r["design"]]
            for key, _l, _s, _d, lib in [(m[0], m[1], m[2], m[3], m[4])
                                         for m in METRICS]:
                tns = (key == "total_negative_slack")
                b, c = num(B, key), num(R, key)
                p = pct_delta(b, c, lib, tns)
                row += [b, c, (None if p is None else round(p, 3))]
            row += [
                (R or {}).get("mbff_count"),
                (R or {}).get("mbff_ratio"),
                (R or {}).get("half_connected_pin_pairs"),
                len((R or {}).get("missing_comb_cells", []) or []),
                r["report_path"], r["baseline_path"],
            ]
            w.writerow(row)


def main():
    ap = argparse.ArgumentParser(description="MP1 PPA table (baseline vs "
                                             "clustered) for the 7 designs")
    ap.add_argument("--designs", nargs="+", default=DESIGNS)
    ap.add_argument("--report-dir", default="result",
                    help="dir holding <design>_report.json (default: result)")
    ap.add_argument("--baseline-dir", default="runs",
                    help="dir holding <design>/baseline.json (default: runs)")
    ap.add_argument("--report-glob", default=None,
                    help="template, e.g. 'out/{design}.json' (overrides dir)")
    ap.add_argument("--baseline-glob", default=None,
                    help="template, e.g. 'base/{design}.json' (overrides dir)")
    ap.add_argument("--md", default="ppa_table.md",
                    help="markdown output path ('' to skip)")
    ap.add_argument("--csv", default="ppa_table.csv",
                    help="csv output path ('' to skip)")
    ap.add_argument("--no-files", action="store_true",
                    help="print only; do not write md/csv")
    a = ap.parse_args()

    rows = collect(a)

    found = sum(1 for r in rows if r["R"] is not None)
    print(render_text(rows))
    print()
    miss = [r["design"] for r in rows if r["R"] is None]
    if miss:
        print(f"note: no clustered report found for: {', '.join(miss)}")
    nob = [r["design"] for r in rows
           if r["R"] is not None and r["B"] is None]
    if nob:
        print(f"note: no baseline found for: {', '.join(nob)} "
              f"(delta% shown as n/a for those)")
    print(f"resolved {found}/{len(rows)} clustered reports.")

    if not a.no_files:
        if a.md:
            with open(a.md, "w") as fh:
                fh.write("# MP1 PPA summary (baseline -> clustered)\n\n")
                fh.write(render_markdown(rows) + "\n\n")
                fh.write("_Lower is better for all metrics; TNS compared on "
                         "magnitude. A negative delta% is an improvement._\n")
            print(f"wrote {a.md}")
        if a.csv:
            render_csv(rows, a.csv)
            print(f"wrote {a.csv}")


if __name__ == "__main__":
    main()