#!/usr/bin/env bash
# Toolshed spike: setpriv + prlimit in rootless podman, the meter socket mount (root yes, tool no), the /work mount.
# Usage: bash spikes/spike_shed.sh [image]   (make spike-shed). Uses throwaway containers and temp dirs only.
set -u
IMAGE=${1:-localhost/taltempla-shed:latest}
podman image exists "$IMAGE" || IMAGE=docker.io/library/python:3.13-slim
TMP=$(mktemp -d /tmp/taltempla-spike.XXXXXX)
trap 'kill $SRV 2>/dev/null; podman unshare rm -rf "$TMP" 2>/dev/null || rm -rf "$TMP"' EXIT
fail=0
res() { if [ "$1" = 0 ]; then printf 'PASS %s\n' "$2"; else printf 'FAIL %s  (%s)\n' "$2" "${3:-}"; fail=1; fi; }
run() { podman run --rm --pids-limit 256 "$@"; }

out=$(run "$IMAGE" setpriv --reuid=1000 --regid=1000 --clear-groups --no-new-privs -- id -u 2>&1)
[ "$out" = 1000 ]; res $? "setpriv --reuid=1000 in rootless podman" "$out"

out=$(run "$IMAGE" prlimit --as=2147483648 --nproc=512 --nofile=1024 -- timeout -k 2 5 python3 -c 'print("ok")' 2>&1)
[ "$out" = ok ]; res $? "prlimit + timeout around python3" "$out"

# Meter socket: dir 0700 + socket 0600 on the host, bind-mounted at /run/meter.
mkdir -p "$TMP/run" && chmod 700 "$TMP/run"
python3 - "$TMP/run/meter.sock" <<'PY' &
import os, socket, sys
p = sys.argv[1]
s = socket.socket(socket.AF_UNIX); s.bind(p); os.chmod(p, 0o600); s.listen(4)
while True:
    c, _ = s.accept(); c.sendall(b"pong\n"); c.close()
PY
SRV=$!
for _ in $(seq 20); do [ -S "$TMP/run/meter.sock" ] && break; sleep 0.1; done
probe='import socket; s=socket.socket(socket.AF_UNIX); s.connect("/run/meter/meter.sock"); print(s.recv(10).decode().strip())'
out=$(run -v "$TMP/run:/run/meter" "$IMAGE" python3 -c "$probe" 2>&1)
[ "$out" = pong ]; res $? "container root reaches the meter socket" "$out"
out=$(run -v "$TMP/run:/run/meter" "$IMAGE" setpriv --reuid=1000 --regid=1000 --clear-groups -- python3 -c "$probe" 2>&1)
echo "$out" | grep -q PermissionError; res $? "tool user gets EACCES on the meter socket" "$out"

# /work: root makes out/ 1777, the tool user writes there, the host sees the file.
mkdir -p "$TMP/work"
out=$(run -v "$TMP/work:/work" "$IMAGE" sh -c 'mkdir -p /work/out && chmod 1777 /work/out &&
      setpriv --reuid=1000 --regid=1000 --clear-groups -- sh -c "echo hi > /work/out/t.txt" && cat /work/out/t.txt' 2>&1)
[ "$out" = hi ] && [ -f "$TMP/work/out/t.txt" ]; res $? "/work mount: tool user writes /work/out, host sees it" "$out"

# The published admin port answers on the host loopback (only if the toolshed runs).
if curl -fsS --max-time 2 http://127.0.0.1:7700/health >/dev/null 2>&1; then res 0 "toolshed :7700 answers on 127.0.0.1"
else echo "SKIP toolshed :7700 (not running)"; fi

[ $fail = 0 ] && echo "spike-shed: all checks passed" || echo "spike-shed: FAILURES"
exit $fail
