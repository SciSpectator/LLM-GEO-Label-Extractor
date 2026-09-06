#!/bin/bash
set -u
cd Directory to working data dir
source Directory to working data dir/env.sh

for i in $(seq 0 7); do
  curl -s -m 3 "http://127.0.0.1:$((8000+i))/v1/models" >/dev/null 2>&1 || {
    echo "REFUSING: replica $((8000+i)) is not serving" >&2; exit 1; }
done

mkdir -p Directory to working data dir/p2out
if [ -f Directory to working data dir/p2out/RUNNING ]; then
  echo "REFUSING: a run is already in progress" >&2; exit 1
fi
touch Directory to working data dir/p2out/RUNNING
trap 'rm -f Directory to working data dir/p2out/RUNNING' EXIT

exec python3 run_phase2.py \
  --corpus "Directory to working data dir/p1_[0-7].json" \
  --out-dir Directory to working data dir/p2out \
  --vocab Directory to vocab.sqlite file \
  --index Directory to vocab index file \
  --cellosaurus Directory to cellosaurus.sqlite file \
  --workers 512
