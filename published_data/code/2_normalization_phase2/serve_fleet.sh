#!/bin/bash
set -u
source Directory to Python environment/bin/activate
ulimit -n 65536
MODEL=${MODEL:-Directory to model weights}
LOGDIR=${LOGDIR:-Directory to working data dir}

up() { curl -s -m 3 "http://127.0.0.1:$((8000+$1))/v1/models" >/dev/null 2>&1; }

if [ "${RESTART:-0}" = "1" ]; then
  for g in $(seq 0 7); do
    for p in $(nvidia-smi -i "$g" --query-compute-apps=pid --format=csv,noheader); do
      kill -9 "$p" 2>/dev/null
    done
  done
  sleep 10
  echo "stopped; gpu mem: $(nvidia-smi --query-gpu=memory.used --format=csv,noheader | tr '\n' ' ')"
fi

for pair in "0 1" "2 3" "4 5" "6 7"; do
  for i in $pair; do
    up "$i" && { echo "  gpu$i already serving"; continue; }
    CUDA_VISIBLE_DEVICES=$i VLLM_USE_V2_MODEL_RUNNER=0 setsid nohup \
      python3 -m vllm.entrypoints.openai.api_server \
      --model "$MODEL" --served-model-name gemma4-e2b-text \
      --max-model-len 8192 --gpu-memory-utilization 0.90 \
      --limit-mm-per-prompt '{"image":0,"audio":0}' \
      --reasoning-parser gemma4 --port $((8000+i)) \
      >"$LOGDIR/vllm_$i.log" 2>&1 </dev/null &
    disown
  done
  for t in $(seq 1 90); do
    ok=1; for i in $pair; do up "$i" || ok=0; done
    [ "$ok" = 1 ] && break
    sleep 5
  done
  n=0; for i in $pair; do up "$i" && n=$((n+1)); done
  echo "  pair [$pair]: $n/2 up"
done

n=0; for i in $(seq 0 7); do up "$i" && n=$((n+1)); done
echo "FLEET_READY $n/8"
