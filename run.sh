set -uo pipefail
 
OPENROAD="${OPENROAD:-openroad}"
 
# The 7 official MP1 designs (ECE_260C_MP1 pp.2-3). Override by passing names.
ALL_DESIGNS=(gcd_v1 ibex_v1 ibex_v2 jpeg_v1 jpeg_v2 riscv32i_v1 riscv32i_v2)
if [[ $# -gt 0 ]]; then
  DESIGNS=("$@")
else
  DESIGNS=("${ALL_DESIGNS[@]}")
fi
 
# Run from the repo root (the directory containing clustering.py).
cd "$(dirname "$0")"
if [[ ! -f clustering.py || ! -f evaluator.py ]]; then
  echo "ERROR: run this from the repo root (clustering.py + evaluator.py must be here)." >&2
  exit 1
fi
 
mkdir -p result
 
ok=()      # designs that finished both steps
failed=()  # designs where clustering or evaluation failed
 
for D in "${DESIGNS[@]}"; do
  echo
  echo "============================================================"
  echo ">>> $D"
  echo "============================================================"
 
  if [[ ! -f "designs/$D/design.odb" ]]; then
    echo "  SKIP: designs/$D/design.odb not found."
    failed+=("$D (missing design.odb)")
    continue
  fi
 
  mkdir -p "runs/$D"
 
  # ---- 1. Clustering --------------------------------------------------------
  echo "--- clustering ($D) ---"
  t0=$SECONDS
  "$OPENROAD" -exit -python clustering.py --design "$D" \
      2>&1 | tee "result/${D}_cluster.log"
  crc=${PIPESTATUS[0]}
  cdur=$((SECONDS - t0))
  if [[ $crc -ne 0 ]]; then
    echo "  clustering.py FAILED for $D (exit $crc) after ${cdur}s -- see result/${D}_cluster.log"
    failed+=("$D (clustering)")
    continue
  fi
  if [[ ! -f "runs/$D/clustered.odb" ]]; then
    echo "  ERROR: runs/$D/clustered.odb was not produced."
    failed+=("$D (no clustered.odb)")
    continue
  fi
  echo "  clustering OK in ${cdur}s"
 
  # ---- 2. Evaluation --------------------------------------------------------
  echo "--- evaluation ($D) ---"
  t0=$SECONDS
  "$OPENROAD" -exit -python evaluator.py --design "$D" \
      --input "runs/$D/clustered.odb" \
      --sdc   "designs/$D/constraints.sdc" \
      --output "result/${D}_report.json" \
      2>&1 | tee "result/${D}_report.log"
  erc=${PIPESTATUS[0]}
  edur=$((SECONDS - t0))
  if [[ $erc -ne 0 ]]; then
    echo "  evaluator.py FAILED for $D (exit $erc) after ${edur}s -- see result/${D}_report.log"
    failed+=("$D (evaluation)")
    continue
  fi
  echo "  evaluation OK in ${edur}s -> result/${D}_report.json"
 
  ok+=("$D")
done
 
# ---- Summary ----------------------------------------------------------------
echo
echo "============================================================"
echo "SUMMARY"
echo "============================================================"
echo "Completed (${#ok[@]}): ${ok[*]:-none}"
if [[ ${#failed[@]} -gt 0 ]]; then
  echo "Failed   (${#failed[@]}):"
  for f in "${failed[@]}"; do echo "  - $f"; done
  exit 1
fi
echo "All designs completed successfully."
 