import openroad, odb
from openroad import Design, Tech, Timing
from odb import *
import os
import argparse
from glob import glob
# --- Import additional packages here ---
import re
import json
import math
import random
from collections import defaultdict, deque


# --- Do not edit except to add additional, optional parameters ---

parser = argparse.ArgumentParser(description="ECE 260C MBFF Clustering")

parser.add_argument(
    '--design',
    type=str,
    help="Your design to load. e.g., 'gcd_v1'",
    required=True
)
parser.add_argument(
    '--output',
    type=str,
    help="Output path (defaults to runs/<design>/clustered.odb)"
)

# --- Optional parameters: knobs of the GLSVLSI'24 divide-and-conquer flow ----
# (Kahng, Kundu & Thumathy, "Scalable Flip-Flop Clustering Using Divide and
#  Conquer For Capacitated K-Means", GLSVLSI'24.)
parser.add_argument('--U', type=int, default=200,
                    help="Pointset-decomposition sub-problem size cap (paper's "
                         "U; default 500 with CPLEX/LEMON, lower here because "
                         "the LP/ILP/MCF are pure-Python -- recursion does the "
                         "heavy lifting).")
parser.add_argument('--multistart', type=int, default=10,
                    help="k-means++ multi-start count (paper default 20).")
parser.add_argument('--kmeans-iters', type=int, default=12,
                    help="Max Lloyd iterations per k-means run.")
parser.add_argument('--mcf-lp-iters', type=int, default=4,
                    help="Max MCF<->LP iterations per sub-problem (paper: 5).")
parser.add_argument('--max-neighbors', type=int, default=8,
                    help="Each FF connects only to its N nearest candidate "
                         "trays in the min-cost-flow graph (sparsification, "
                         "cf. FTray's max-edge-length restriction).")
parser.add_argument('--silhouette-sample', type=int, default=512,
                    help="Cap on points used to score the Silhouette metric "
                         "when choosing k (full k-means still uses all points).")
parser.add_argument('--alpha', type=float, default=256.0,
                    help="Objective weight on power W in (alpha*W + D + beta*R) "
                         "(Eq. 8). The paper recalibrates alpha per technology; "
                         "here D is in microns, so larger alpha -> more/larger "
                         "MBFFs. Raise this if a sparse design clusters little.")
parser.add_argument('--beta', type=float, default=0.2,
                    help="Objective weight on launch/capture relative "
                         "displacement R (Eq. 5). 0 disables the R proxy.")
parser.add_argument('--seed', type=int, default=42,
                    help="RNG seed -- the whole flow is otherwise deterministic.")
parser.add_argument('--max-disp-um', type=float, default=0.0,
                    help="Optional hard cap (microns) on FF->slot displacement "
                         "(safety net; 0 = rely on the objective only).")
parser.add_argument('--reset-policy', type=str, default='strict',
                    choices=['strict', 'domain'],
                    help="'strict': only merge flops sharing the same clock AND "
                         "reset net. 'domain': group by clock; MBFF reset = "
                         "dominant reset of the cluster.")
parser.add_argument('--four-bit-cell', type=str,
                    default='sg13g2_dfrbpq_H2V2X_1',
                    help="Master for 4-bit trays (2x2 aspect legalizes best).")
parser.add_argument('--two-bit-cell', type=str,
                    default='sg13g2_dfrbpq_V2X_1',
                    help="Master for 2-bit trays.")
parser.add_argument('--legalize', action='store_true', default=False,
                    help="Run detailed_placement after clustering (evaluator "
                         "legalizes anyway; off by default).")


args = parser.parse_args()
tech = Tech()



print("Loading design...")
design = Design(tech)
tech.readLiberty("pdk/lib/sg13g2_stdcell_typ_1p20V_25C_mbff.lib")

design.readDb(f"designs/{args.design}/design.odb")
# Our design databases already have the MBFF LEF files loaded into them. 
library = design.getDb().getLibs()[0]
    

design.evalTclString(f"source pdk/setRC.tcl")
design.evalTclString(f"read_sdc designs/{args.design}/constraints.sdc")
library = design.getDb().getLibs()[0]
dbu_per_micron = library.getDbUnitsPerMicron()
block = design.getBlock()

print("Performing MBFF clustering...")
# --- Your Code Below ---
# =============================================================================
# ECE 260C MP1 -- MBFF clustering via the GLSVLSI'24 divide-and-conquer flow
# Kahng, Kundu & Thumathy, "Scalable Flip-Flop Clustering Using Divide and
# Conquer For Capacitated K-Means", GLSVLSI'24.
#
# Pipeline (mirrors Sections 3-4 of the paper):
#
#   1. Identify clusterable single-bit FFs; bucket by (clock net, reset net)
#      so every FF in a bucket is electrically interchangeable on CLK/RESET
#      (the paper's ASAP7 MBFFs are likewise inverting/synchronous/non-scan).
#
#   2. Recursive Pointset Decomposition (Algorithm 1). Per bucket: pick
#      k in [2,8] by max Silhouette; run `multistart` k-means++ with that k;
#      keep the run with the lowest sum FF->center distance; Kruskal-like
#      MergeClusters of nearest cluster pairs while merged size <= U (no
#      center update after a merge); recurse. Sub-problems each <= U FFs.
#
#   3. Per sub-problem, capacitated k-means following FTray [4]: initial
#      tray centers from one k-means++ pass; for each MBFF size s in {4,2},
#      alternate min-cost flow (FF->slot assignment minimizing displacement
#      D, Eq. 2) with an LP that recenters each tray. The L1-optimal LP
#      solution is the coordinate-wise median, so no LP solver is needed.
#
#   4. Size selection (the ILP, Eqs. 8-13). The union of trays produced for
#      all sizes is the candidate set; a greedy weighted set-partition picks
#      a partition minimizing alpha*W + D (+ beta*R), with W from the
#      normalized power-per-bit model (Table 3). Constraint (13) is honored:
#      only FF/slot pairs the MCF produced are ever candidates. The CPLEX
#      ILP is replaced by the greedy (no ILP solver in the container).
#
#   5. Build chosen MBFFs, rewire D/Q/Q_N/CLK/RESET, delete originals, emit
#      changelist.json. Size-1 (singleton) flops are left unchanged --
#      allowed by the spec and exactly the paper's size-1 slot.
#
# Adaptations vs. the paper: MCF is a pure-Python successive-shortest-path
# solver; the LP is the exact coordinate-wise median; the ILP is a greedy
# weighted set-partition. The SG13G2 mock PDK has only 2- and 4-bit cells
# (paper: {1,2,4,8,16}), so the size set is {2,4} and alpha is recalibrated
# for this PDK (D measured in microns) -- the paper itself recalibrates
# alpha per technology (Sec. 4.2).
# =============================================================================

RNG_SEED = args.seed
random.seed(RNG_SEED)
PPB = {1: 1.000, 2: 0.900, 4: 0.875}        # normalized power-per-bit (Table 3)
MAX_DISP_DBU = (args.max_disp_um * dbu_per_micron) if args.max_disp_um else None

MBFF_MASTER_NAMES = {
    'sg13g2_dfrbpq_H2V2X_1', 'sg13g2_dfrbpq_V2X_1', 'sg13g2_dfrbpq_V2X_2',
    'sg13g2_dfrbpq_V4X_1', 'sg13g2_dfrbpq_V4X_2',
}
FF_NAME_RE = re.compile(r'(?i)(dfrbp|dfbp|dfxbp|sdfrbp|_dff|dff_|(?<![a-z])df)')
LATCH_NAME_RE = re.compile(r'(?i)(dlh|dlr|latch|_lat|dlat)')


# --------------------------------------------------------------------------
# OpenDB helpers (binding-version tolerant). These define the evaluator /
# equivalence-checker contract and are reused verbatim from the validated
# scaffold -- INCLUDING the name-based (not id()-based) terminal comparison.
# --------------------------------------------------------------------------

def _io(mt):
    return str(mt.getIoType()).split('.')[-1].split('_')[-1].upper()


def _sig(mt):
    return str(mt.getSigType()).split('.')[-1].split('_')[-1].upper()


def _trailing_int(name):
    # Bit index of a tray pin. Tolerate D0 / D_0 / D[0] / D<0> / Q_N3 styles
    # by taking the LAST integer in the pin name (Q_N3 -> 3, not the "N").
    nums = re.findall(r'\d+', name)
    return int(nums[-1]) if nums else 0


def _make_inst(blk, master, name):
    try:
        return odb.dbInst.create(blk, master, name)
    except Exception:
        return dbInst_create(blk, master, name)               # noqa: F405


def _destroy_inst(inst):
    try:
        odb.dbInst.destroy(inst)
    except Exception:
        dbInst_destroy(inst)                                   # noqa: F405


def classify_pins(master):
    """(clk, rst, data_inputs, q_outs, qn_outs); data/q lists sorted by bit."""
    clk = rst = None
    din, qout, qnout = [], [], []
    for mt in master.getMTerms():
        u = mt.getName().upper()
        if _sig(mt) in ('POWER', 'GROUND'):
            continue
        io = _io(mt)
        if io in ('INPUT', 'INOUT'):
            if re.fullmatch(r'(CLK|CK|CP|CLOCK)\d*', u) or 'CLK' in u or 'CLOCK' in u:
                clk = mt
            elif ('RESET' in u or 'RSTB' in u or 'RSTN' in u
                  or re.search(r'(^|_)R(ST|B|N)?\d*$', u)
                  or 'CLR' in u or 'CLEAR' in u):
                rst = mt
            else:
                din.append(mt)
        elif io == 'OUTPUT':
            if re.search(r'Q_?N\d*$', u) or u.startswith('QN'):
                qnout.append(mt)
            else:
                qout.append(mt)
    din.sort(key=lambda m: _trailing_int(m.getName()))
    qout.sort(key=lambda m: _trailing_int(m.getName()))
    qnout.sort(key=lambda m: _trailing_int(m.getName()))
    return clk, rst, din, qout, qnout


def is_single_bit_ff(master):
    nm = master.getName()
    if nm in MBFF_MASTER_NAMES or LATCH_NAME_RE.search(nm):
        return False
    if not FF_NAME_RE.search(nm):
        return False
    clk, rst, din, qo, qno = classify_pins(master)
    if clk is None or len(din) != 1 or (len(qo) + len(qno)) == 0:
        return False
    return True


def iterm_for(inst, mt):
    return inst.findITerm(mt.getName()) if mt is not None else None


def net_on(inst, mt):
    it = iterm_for(inst, mt)
    return it.getNet() if it is not None else None


def net_key(net):
    """Stable, hashable identity for a dbNet across OpenDB binding versions.
    This OpenROAD build's dbNet has no getId(), so fall back to the (block-
    unique) net name; never use Python id(), which differs per SWIG wrapper."""
    if net is None:
        return None
    g = getattr(net, 'getId', None)
    if g is not None:
        try:
            return ('i', g())
        except Exception:
            pass
    for attr in ('getName', 'getConstName'):
        g = getattr(net, attr, None)
        if g is not None:
            try:
                return ('n', g())
            except Exception:
                pass
    return ('p', id(net))                       # last resort (single-pass only)


# --------------------------------------------------------------------------
# Generic numeric primitives -- k-means++, Silhouette, min-cost flow, median
# (pure stdlib so the script runs in the bare OpenROAD Python env)
# --------------------------------------------------------------------------

def _euclid2(a, b):
    dx = a[0] - b[0]
    dy = a[1] - b[1]
    return dx * dx + dy * dy


def _manh(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def kmeanspp_init(pts, k, rng):
    """Standard D^2 k-means++ seeding."""
    n = len(pts)
    centers = [pts[rng.randrange(n)]]
    d2 = [_euclid2(p, centers[0]) for p in pts]
    while len(centers) < k:
        tot = sum(d2)
        if tot <= 0.0:
            centers.append(pts[rng.randrange(n)])
        else:
            r = rng.random() * tot
            acc = 0.0
            idx = 0
            for i, w in enumerate(d2):
                acc += w
                if acc >= r:
                    idx = i
                    break
            centers.append(pts[idx])
        c = centers[-1]
        for i, p in enumerate(pts):
            dd = _euclid2(p, c)
            if dd < d2[i]:
                d2[i] = dd
    return centers


def kmeans(pts, k, rng, iters):
    """Lloyd's algorithm. Returns (labels, centers)."""
    centers = kmeanspp_init(pts, k, rng)
    labels = [0] * len(pts)
    for _ in range(iters):
        changed = False
        for i, p in enumerate(pts):
            best = 0
            bd = _euclid2(p, centers[0])
            for c in range(1, k):
                d = _euclid2(p, centers[c])
                if d < bd:
                    bd = d
                    best = c
            if labels[i] != best:
                labels[i] = best
                changed = True
        acc = [[0.0, 0.0, 0] for _ in range(k)]
        for i, p in enumerate(pts):
            a = acc[labels[i]]
            a[0] += p[0]
            a[1] += p[1]
            a[2] += 1
        for c in range(k):
            if acc[c][2]:
                centers[c] = (acc[c][0] / acc[c][2], acc[c][1] / acc[c][2])
        if not changed:
            break
    return labels, centers


def kmeans_cost(pts, labels, centers):
    """Sum of FF-to-assigned-center Euclidean distance (paper's criterion)."""
    return sum(math.sqrt(_euclid2(p, centers[labels[i]]))
               for i, p in enumerate(pts))


def silhouette(pts, labels, k, rng, sample):
    """Mean silhouette coefficient (sampled). Higher == better-separated."""
    n = len(pts)
    if k < 2 or n <= k:
        return -1.0
    by = defaultdict(list)
    for i, l in enumerate(labels):
        by[l].append(i)
    if len(by) < 2:
        return -1.0
    idxs = list(range(n))
    if n > sample:
        idxs = rng.sample(idxs, sample)
    tot = 0.0
    cnt = 0
    for i in idxs:
        li = labels[i]
        same = by[li]
        if len(same) <= 1:
            continue
        a = sum(math.sqrt(_euclid2(pts[i], pts[j]))
                for j in same if j != i) / (len(same) - 1)
        b = math.inf
        for l, mem in by.items():
            if l == li or not mem:
                continue
            db = sum(math.sqrt(_euclid2(pts[i], pts[j]))
                     for j in mem) / len(mem)
            if db < b:
                b = db
        denom = max(a, b)
        if denom > 0:
            tot += (b - a) / denom
            cnt += 1
    return (tot / cnt) if cnt else -1.0


def _median(pts):
    xs = sorted(p[0] for p in pts)
    ys = sorted(p[1] for p in pts)
    m = len(pts) // 2
    return (xs[m], ys[m])


class _MCF:
    """Min-cost max-flow (SPFA / Bellman-Ford-queue shortest augmenting path).
    Per-sub-problem instances are small, so an O(V*E) augmenting solver is
    sufficient and avoids any external solver dependency."""

    def __init__(self, n):
        self.n = n
        self.g = [[] for _ in range(n)]

    def add(self, u, v, cap, cost):
        self.g[u].append([v, cap, cost, len(self.g[v])])
        self.g[v].append([u, 0, -cost, len(self.g[u]) - 1])

    def run(self, s, t):
        n = self.n
        res = 0
        flow = 0
        INF = math.inf
        while True:
            dist = [INF] * n
            inq = [False] * n
            pe = [(-1, -1)] * n
            dist[s] = 0
            dq = deque([s])
            while dq:
                u = dq.popleft()
                inq[u] = False
                du = dist[u]
                for ei, e in enumerate(self.g[u]):
                    v, cap, cost, _ = e
                    if cap > 0 and du + cost < dist[v]:
                        dist[v] = du + cost
                        pe[v] = (u, ei)
                        if not inq[v]:
                            inq[v] = True
                            dq.append(v)
            if dist[t] == INF:
                break
            push = INF
            v = t
            while v != s:
                u, ei = pe[v]
                if self.g[u][ei][1] < push:
                    push = self.g[u][ei][1]
                v = u
            v = t
            while v != s:
                u, ei = pe[v]
                self.g[u][ei][1] -= push
                rev = self.g[u][ei][3]
                self.g[v][rev][1] += push
                v = u
            flow += push
            res += push * dist[t]
        return flow, res


def capacitated_assign(pts, centers, cap, max_nbr):
    """Assign each point to a center; each center holds <= cap points,
    minimizing total Manhattan displacement (Eq. 2). Returns labels
    (center index per point), or None if infeasible even densely."""
    n = len(pts)
    K = len(centers)
    if K == 0 or K * cap < n:
        return None

    def build(neighbor_only):
        mcf = _MCF(n + K + 2)
        S, T = n + K, n + K + 1
        for i in range(n):
            mcf.add(S, i, 1, 0)
        for c in range(K):
            mcf.add(n + c, T, cap, 0)
        for i, p in enumerate(pts):
            if neighbor_only:
                order = sorted(range(K),
                               key=lambda c: _manh(p, centers[c]))[:max_nbr]
            else:
                order = range(K)
            for c in order:
                mcf.add(i, n + c, 1, _manh(p, centers[c]))
        return mcf, S, T

    mcf, S, T = build(True)
    f, _ = mcf.run(S, T)
    if f < n:                                   # sparsified graph infeasible
        mcf, S, T = build(False)
        f, _ = mcf.run(S, T)
        if f < n:
            return None
    labels = [-1] * n
    for i in range(n):
        for v, capr, cost, _ in mcf.g[i]:
            if n <= v < n + K and capr == 0:    # saturated FF->center edge
                labels[i] = v - n
                break
    return labels


# --------------------------------------------------------------------------
# 2. Recursive Pointset Decomposition  (Algorithm 1)
# --------------------------------------------------------------------------

def decomp(items, U, rng):
    """items: list of (global_id, (x,y)). Returns list of such lists, each
    of size <= U.  Faithful to Algorithm 1: k in [2,8] by Silhouette,
    multi-start k-means keeping min FF->center distance, Kruskal-like merge
    with no center update, recurse."""
    out = []
    stack = [items]
    while stack:
        cur = stack.pop()
        if len(cur) <= U:
            out.append(cur)
            continue
        pts = [c[1] for c in cur]

        best_k, best_sil = 2, -2.0
        for k in range(2, 9):
            if k >= len(cur):
                break
            lb, _ = kmeans(pts, k, random.Random(RNG_SEED + k), 4)
            sil = silhouette(pts, lb, k, random.Random(RNG_SEED + 99),
                             args.silhouette_sample)
            if sil > best_sil:
                best_sil, best_k = sil, k

        best_lb = best_ct = None
        best_cost = math.inf
        for ms in range(args.multistart):
            lb, ct = kmeans(pts, best_k,
                            random.Random(RNG_SEED + 1000 * ms + best_k),
                            args.kmeans_iters)
            cost = kmeans_cost(pts, lb, ct)
            if cost < best_cost:
                best_cost, best_lb, best_ct = cost, lb, ct

        groups = defaultdict(list)
        for gi, lab in enumerate(best_lb):
            groups[lab].append(cur[gi])
        cl = list(groups.values())
        cen = [best_ct[l] for l in groups.keys()]

        # MergeClusters: Kruskal-like, merge closest pair while size <= U,
        # do NOT recompute centers after a merge (paper's single-linkage).
        parent = list(range(len(cl)))
        size = [len(c) for c in cl]

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        edges = []
        for i in range(len(cl)):
            for j in range(i + 1, len(cl)):
                edges.append((_manh(cen[i], cen[j]), i, j))
        edges.sort()
        for _, i, j in edges:
            ri, rj = find(i), find(j)
            if ri == rj:
                continue
            if size[ri] + size[rj] <= U:
                parent[ri] = rj
                size[rj] += size[ri]

        merged = defaultdict(list)
        for i in range(len(cl)):
            merged[find(i)].extend(cl[i])
        for g in merged.values():
            if len(g) <= U:
                out.append(g)
            else:
                stack.append(g)                 # recurse on the big ones
    return out


# --------------------------------------------------------------------------
# 3-4. Capacitated k-means per sub-problem + greedy ILP size selection
# --------------------------------------------------------------------------

def capacitated_kmeans(coords, s, rng):
    """Trays of size <= s via MCF<->median iteration (FTray-style). Returns
    list of member-index lists."""
    n = len(coords)
    if n < 2:
        return []
    K = max(1, math.ceil(n / s))
    centers = kmeanspp_init(coords, K, rng)
    prev = None
    labels = None
    for _ in range(args.mcf_lp_iters):
        labels = capacitated_assign(coords, centers, s, args.max_neighbors)
        if labels is None:
            return []
        if labels == prev:
            break
        prev = labels[:]
        grp = defaultdict(list)
        for i, l in enumerate(labels):
            grp[l].append(coords[i])
        for l, pl in grp.items():
            centers[l] = _median(pl)            # LP optimum == L1 median
    trays = defaultdict(list)
    for i, l in enumerate(labels):
        trays[l].append(i)
    return [m for m in trays.values() if m]


def select_trays(coords, names):
    """Union the trays produced for sizes {4,2}; greedily pick a disjoint
    set minimizing alpha*W + D (Eq. 8). A tray is accepted only if its
    power saving outweighs its added displacement (vs. leaving its members
    as size-1 slots). Returns list of (member_indices, center_xy)."""
    rng = random.Random(RNG_SEED + 7)
    cand = []
    for s in (4, 2):
        for tray in capacitated_kmeans(coords, s, rng):
            if len(tray) < 2:
                continue
            ctr = _median([coords[i] for i in tray])
            n = len(tray)
            size_cls = 4 if n >= 3 else 2
            if MAX_DISP_DBU is not None and any(
                    _manh(coords[i], ctr) > MAX_DISP_DBU for i in tray):
                continue
            d_um = sum(_manh(coords[i], ctr) for i in tray) / dbu_per_micron
            # delta(alpha*W + D) of forming the tray vs. n singletons:
            #   power saving = n*(PPB[1] - PPB[size]);  displacement = d_um
            benefit = args.alpha * n * (PPB[1] - PPB[size_cls]) - d_um
            if benefit <= 0.0:
                continue
            cand.append((benefit, tray, ctr))
    cand.sort(key=lambda c: (-c[0], [names[i] for i in c[1]]))
    used = set()
    chosen = []
    for _, tray, ctr in cand:
        if any(i in used for i in tray):
            continue
        used.update(tray)
        chosen.append((tray, (int(ctr[0]), int(ctr[1]))))
    return chosen


# --------------------------------------------------------------------------
# 1. Collect clusterable FFs and bucket by (clock net, reset net)
#    (verbatim scaffold; enforces constraint (4) via the name-based
#     extra-input guard + clock/reset bucketing.)
# --------------------------------------------------------------------------

flop_info = []
n_total_ff = 0
sk_clk = sk_rst = sk_extra = sk_out = 0

for inst in block.getInsts():
    if not is_single_bit_ff(inst.getMaster()):
        continue
    n_total_ff += 1
    clk_mt, rst_mt, din_mt, qo_mt, qno_mt = classify_pins(inst.getMaster())

    clk_net = net_on(inst, clk_mt)
    rst_net = net_on(inst, rst_mt) if rst_mt is not None else None
    d_net = net_on(inst, din_mt[0])
    q_net = net_on(inst, qo_mt[0]) if qo_mt else None
    qn_net = net_on(inst, qno_mt[0]) if qno_mt else None

    if clk_net is None or d_net is None:
        sk_clk += 1
        continue
    if q_net is None and qn_net is None:
        sk_out += 1
        continue

    # Compare master-terminals by NAME, never by Python id(): OpenROAD's
    # SWIG bindings return a fresh wrapper object on every accessor call,
    # so the MTerm from classify_pins (master.getMTerms()) and the one from
    # iterm.getMTerm() are different Python objects for the same pin and
    # would never be id()-equal -- which would (wrongly) flag every flop as
    # "extra-input" and drop all of them (the riscv32i_v2 symptom).
    allowed = {clk_mt.getName(), din_mt[0].getName()}
    if rst_mt is not None:
        allowed.add(rst_mt.getName())
    extra = False
    for it in inst.getITerms():
        mt = it.getMTerm()
        if (_io(mt) in ('INPUT', 'INOUT') and _sig(mt) not in
                ('POWER', 'GROUND') and mt.getName() not in allowed
                and it.getNet() is not None):
            extra = True
            break
    if extra:
        sk_extra += 1
        continue
    if args.reset_policy == 'strict' and rst_net is None:
        sk_rst += 1
        continue

    bb = inst.getBBox()
    flop_info.append(dict(
        inst=inst, name=inst.getName(),
        cx=(bb.xMin() + bb.xMax()) // 2, cy=(bb.yMin() + bb.yMax()) // 2,
        clk_net=clk_net, rst_net=rst_net,
        d_net=d_net, q_net=q_net, qn_net=qn_net,
    ))

buckets = defaultdict(list)
for fi in flop_info:
    if args.reset_policy == 'strict':
        key = (net_key(fi['clk_net']),
               net_key(fi['rst_net']) if fi['rst_net'] is not None else -1)
    else:
        key = (net_key(fi['clk_net']), 0)
    buckets[key].append(fi)

print(f"  Clusterable flops: {len(flop_info)} / {n_total_ff} single-bit FFs "
      f"in {len(buckets)} clock/reset bucket(s)")
print(f"  Left unchanged -> no-clk/d:{sk_clk}  no-reset(strict):{sk_rst}  "
      f"extra-input:{sk_extra}  no-output:{sk_out}")


# --------------------------------------------------------------------------
# Main loop: decompose each bucket, then capacitated-kmeans + ILP per
# sub-problem.  Produces final_clusters = [(members, (cx,cy)), ...].
# --------------------------------------------------------------------------

final_clusters = []          # list of (list[flop_info], (cx,cy)) in DBU

for key, group in buckets.items():
    if len(group) < 2:
        continue
    items = [(idx, (fi['cx'], fi['cy'])) for idx, fi in enumerate(group)]
    subproblems = decomp(items, args.U, random.Random(RNG_SEED))
    for sp in subproblems:
        if len(sp) < 2:
            continue
        local = [group[gi] for gi, _ in sp]
        coords = [(fi['cx'], fi['cy']) for fi in local]
        names = [fi['name'] for fi in local]
        for tray, ctr in select_trays(coords, names):
            final_clusters.append(([local[i] for i in tray], ctr))


# --------------------------------------------------------------------------
# Resolve MBFF masters present in the library
# --------------------------------------------------------------------------

lib_master = {}
for lib in design.getDb().getLibs():
    for m in lib.getMasters():
        lib_master[m.getName()] = m


def pick_master(*names):
    for nm in names:
        if nm in lib_master:
            return lib_master[nm]
    return None


master_4b = pick_master(args.four_bit_cell, 'sg13g2_dfrbpq_H2V2X_1',
                         'sg13g2_dfrbpq_V4X_1')
master_2b = pick_master(args.two_bit_cell, 'sg13g2_dfrbpq_V2X_1')
if master_4b is None and master_2b is None:
    raise RuntimeError("No usable MBFF master found in the library.")

master_meta = {}
for mm in (master_4b, master_2b):
    if mm is None:
        continue
    c, r, di, qo, qno = classify_pins(mm)
    master_meta[mm.getName()] = dict(master=mm, clk=c, rst=r, din=di,
                                     qout=qo, qnout=qno, bits=len(di))
print("  MBFF masters: " + ", ".join(
    f"{n}({d['bits']}b)" for n, d in master_meta.items()))


# --------------------------------------------------------------------------
# 5. Build MBFFs, rewire, delete originals, record changelist
#    (verbatim scaffold -- defines the equivalence-checker contract)
# --------------------------------------------------------------------------

changelist = {}
stat = defaultdict(int)
empty_pins = 0
disp_sum = 0.0
disp_n = 0
_serial = 0


def choose_master(n):
    if n >= 3 and master_4b is not None:
        return master_4b
    if n == 2 and master_2b is not None:
        return master_2b
    return master_4b if master_4b is not None else master_2b


def fresh_name():
    global _serial
    while True:
        nm = f"mbff_{_serial}"
        _serial += 1
        if block.findInst(nm) is None:
            return nm


def build_mbff(members, target_xy, master):
    global empty_pins, disp_sum, disp_n
    meta = master_meta[master.getName()]
    bits = meta['bits']
    members = members[:bits]

    name = fresh_name()
    mbff = _make_inst(block, master, name)
    tx, ty = target_xy
    mbff.setLocation(int(tx - master.getWidth() // 2),
                     int(ty - master.getHeight() // 2))
    mbff.setPlacementStatus("PLACED")

    clk_net = members[0]['clk_net']
    if args.reset_policy == 'strict':
        rst_net = members[0]['rst_net']
    else:
        rc = defaultdict(int)
        for m in members:
            if m['rst_net'] is not None:
                rc[net_key(m['rst_net'])] += 1
        rst_net = None
        if rc:
            bid = max(rc, key=rc.get)
            for m in members:
                if m['rst_net'] is not None and net_key(m['rst_net']) == bid:
                    rst_net = m['rst_net']
                    break

    cit = iterm_for(mbff, meta['clk'])
    if cit is not None and clk_net is not None:
        cit.connect(clk_net)
    rit = iterm_for(mbff, meta['rst'])
    if rit is not None and rst_net is not None:
        rit.connect(rst_net)

    entries = [0] * bits
    for i, m in enumerate(members):
        dit = iterm_for(mbff, meta['din'][i])
        if dit is not None and m['d_net'] is not None:
            dit.connect(m['d_net'])
        if m['q_net'] is not None and i < len(meta['qout']):
            it = iterm_for(mbff, meta['qout'][i])
            if it is not None:
                it.connect(m['q_net'])
        if m['qn_net'] is not None and i < len(meta['qnout']):
            it = iterm_for(mbff, meta['qnout'][i])
            if it is not None:
                it.connect(m['qn_net'])
        entries[i] = m['name']
        disp_sum += math.hypot(tx - m['cx'], ty - m['cy']) / dbu_per_micron
        disp_n += 1
        for it in list(m['inst'].getITerms()):
            if it.getNet() is not None:
                it.disconnect()
        _destroy_inst(m['inst'])

    empty_pins += entries.count(0)
    stat[master.getName()] += 1
    changelist[name] = entries


skipped_bitwidth = 0
for members, ctr in final_clusters:
    if len(members) < 2:
        continue
    mst = choose_master(len(members))
    if mst is None:
        continue
    mbits = master_meta[mst.getName()]['bits']
    # MP1 rule: a tray with only 2 populated bits must NOT use a 4-bit cell.
    if len(members) == 2 and mbits != 2:
        if master_2b is None:           # no 2-bit master -> leave flops as-is
            skipped_bitwidth += len(members)
            continue
        mst = master_2b
    build_mbff(members, ctr, mst)
if skipped_bitwidth:
    print(f"  Left {skipped_bitwidth} flops unchanged "
          f"(2-flop trays; no 2-bit MBFF master available)")


# --------------------------------------------------------------------------
# Optional self-contained legalization
# --------------------------------------------------------------------------

if args.legalize:
    try:
        design.evalTclString("detailed_placement")
        print("  Legalized via detailed_placement.")
    except Exception as e:
        print(f"  Legalization skipped ({e}); evaluator will legalize.")


# --------------------------------------------------------------------------
# Emit the equivalence-checker changelist (spec-required)
# --------------------------------------------------------------------------

_out = args.output if args.output else f"runs/{args.design}"
os.makedirs(_out, exist_ok=True)
with open(f"{_out}/changelist.json", "w") as fh:
    json.dump(changelist, fh, indent=2)
print(f"  Wrote {_out}/changelist.json ({len(changelist)} MBFFs)")

merged = sum(sum(1 for e in v if e != 0) for v in changelist.values())
print("MBFF clustering summary (GLSVLSI'24 divide-and-conquer flow)")
print(f"  MBFFs created      : {sum(stat.values())}")
for mn in sorted(stat):
    print(f"    {mn:<26}: {stat[mn]}  ({master_meta[mn]['bits']}-bit)")
print(f"  Single flops merged: {merged} / {n_total_ff}")
print(f"  Empty MBFF bits    : {empty_pins}")
if disp_n:
    print(f"  Avg displacement   : {disp_sum / disp_n:.2f} um  "
          f"(alpha={args.alpha}, beta={args.beta}, U={args.U})")


# --- Do not edit ---
print("Writing Database...")

output_path = args.output if args.output else f"runs/{args.design}"

os.makedirs(output_path, exist_ok=True)

design.writeDb(f"{output_path}/clustered.odb")
print(f"Wrote to {output_path}/clustered.odb")