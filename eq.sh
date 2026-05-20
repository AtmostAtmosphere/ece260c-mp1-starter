#!/usr/bin/env bash
# =============================================================================
# check_equivalence_all.sh
#
# Runs the course logical-equivalence checker for every MP1 design:
#
#   openroad -exit -python equivalence.py \
#       --design <D> --changelist runs/<D>/changelist.json
#
# (equivalence.py's --input defaults to runs/<D>/clustered.odb, and it reads
#  the baseline from designs/<D>/design.odb, so no other args are needed.)
#
# Usage:
#   ./check_equivalence_all.sh                 # all 7 designs
#   ./check_equivalence_all.sh gcd_v1 ibex_v1  # only the listed designs
#   OPENROAD=/full/path/to/openroad ./check_equivalence_all.sh
#
# Notes:
#   - One design failing does NOT stop the others.
#   - equivalence.py does not return a non-zero exit code, so PASS/FAIL is
#     read from runs/<D>/equivalence.json ("results.all_passed"), with a
#     fallback to grepping the log.
#   - Per-design log -> result/<D>_equivalence.log
#   - Overall summary -> result/equivalence_summary.txt  (and stdout)
#   - Script exit code is non-zero if any design FAILED or was SKIPPED.
# =============================================================================
set -uo pipefail

OPENROAD="${OPENROAD:-openroad}"
PYTHON="${PYTHON:-python3}"

ALL_DESIGNS=(gcd_v1 ibex_v1 ibex_v2 jpeg_v1 jpeg_v2 riscv32i_v1 riscv32i_v2)
if [[ $# -gt 0 ]]; then DESIGNS=("$@"); else DESIGNS=("${ALL_DESIGNS[@]}"); fi

# Run from the repo root (where equivalence.py + runs/ + designs/ live).
cd "$(dirname "$0")"
if [[ ! -f equivalence.py ]]; then
  echo "ERROR: run this from the repo root (equivalence.py must be here)." >&2
  exit 1
fi
mkdir -p result

pass=(); fail=(); skip=()

verdict_from_json() {   # $1 = path to equivalence.json -> prints PASS|FAIL:n|?
  "$PYTHON" - "$1" <<'PY' 2>/dev/null || echo "?"
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    r = d.get("results", {})
    if r.get("all_passed"):
        print("PASS")
    else:
        print(f"FAIL:{r.get('failed_count', '?')}")
except Exception:
    print("?")
PY
}

for D in "${DESIGNS[@]}"; do
  CL="runs/$D/changelist.json"
  ODB="runs/$D/clustered.odb"
  LOG="result/${D}_equivalence.log"

  echo
  echo "============================================================"
  echo ">>> $D"
  echo "============================================================"

  if [[ ! -f "$CL" ]]; then
    echo "  SKIP: $CL not found (run clustering.py for $D first)."
    skip+=("$D(no-changelist)")
    continue
  fi
  if [[ ! -f "$ODB" ]]; then
    echo "  SKIP: $ODB not found (run clustering.py for $D first)."
    skip+=("$D(no-clustered-odb)")
    continue
  fi
  if [[ ! -f "designs/$D/design.odb" ]]; then
    echo "  SKIP: designs/$D/design.odb (baseline) not found."
    skip+=("$D(no-baseline)")
    continue
  fi

  t0=$SECONDS
  "$OPENROAD" -exit -python equivalence.py \
      --design "$D" --changelist "$CL" 2>&1 | tee "$LOG"
  dur=$((SECONDS - t0))

  REP="runs/$D/equivalence.json"
  v="?"
  if [[ -f "$REP" ]]; then
    v="$(verdict_from_json "$REP")"
  fi
  if [[ "$v" == "?" ]]; then            # fallback: parse the log
    if grep -q "All checks passed!" "$LOG"; then
      v="PASS"
    elif grep -qE "Failed: [1-9]|Error:" "$LOG"; then
      v="FAIL:?"
    fi
  fi

  case "$v" in
    PASS)   echo "  -> PASS  (${dur}s)";              pass+=("$D") ;;
    FAIL:*) echo "  -> FAIL ${v#FAIL:} (${dur}s) -- see $LOG"; fail+=("$D(${v})") ;;
    *)      echo "  -> UNKNOWN (${dur}s) -- no equivalence.json; see $LOG"
            fail+=("$D(unknown)") ;;
  esac
done

# ---- Summary ---------------------------------------------------------------
SUM="result/equivalence_summary.txt"
{
  echo "MP1 logical-equivalence summary"
  echo "generated: $(date)"
  echo
  printf "  PASS (%d): %s\n" "${#pass[@]}" "${pass[*]:-none}"
  printf "  FAIL (%d): %s\n" "${#fail[@]}" "${fail[*]:-none}"
  printf "  SKIP (%d): %s\n" "${#skip[@]}" "${skip[*]:-none}"
} | tee "$SUM"

echo
if [[ ${#fail[@]} -eq 0 && ${#skip[@]} -eq 0 ]]; then
  echo "All ${#pass[@]} designs PASSED logical equivalence."
  exit 0
fi
echo "Equivalence incomplete: $((${#fail[@]})) failed, $((${#skip[@]})) skipped."
exit 1