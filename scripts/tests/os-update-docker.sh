#!/usr/bin/env bash
# scripts/hasler-os-update against real apt and dpkg, in a Debian trixie arm64
# container (the radio's OS without moOde): private lists, the candidate set,
# holds, deferring when the radio is in use or there is no network, a failing
# health check -> the replaced versions put back with dpkg -i (rebuilt from the
# installed files), pause, and a good night.
#
#   scripts/tests/os-update-docker.sh            # from the repo root; needs docker + network
#
# A fake haslerd answers /api/health on 127.0.0.1:8080: idle while
# /tmp/fake/idle exists, and "mpd" failing while /tmp/fake/break exists and
# the first test package is at its new version (an upgrade that broke the
# radio). Takes ~5 minutes (the real 90 s / 60 s health timings).
set -euo pipefail

if [[ "${1:-}" != --inside ]]; then
  REPO="$(cd "$(dirname "$0")/../.." && pwd)"
  exec docker run --rm --platform linux/arm64 -v "$REPO:/repo:ro" debian:trixie bash /repo/scripts/tests/os-update-docker.sh --inside
fi

say() { printf '\n=== %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*"; exit 1; }
ver() { dpkg-query -W -f '${Version}' "$1"; }
st() { python3 -c "import json,sys; d=json.load(open('/var/lib/hasler-os/status.json')); print(eval(sys.argv[1], {}, d))" "$1"; }
run() { set +e; /usr/bin/python3 -I /usr/local/sbin/hasler-os-update run "$@"; RC=$?; set -e; echo "rc=$RC"; }

say "setup: python3, the updater's health probe, the script"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends python3 ca-certificates >/dev/null
mkdir -p /opt/hasler/updater /usr/local/sbin /tmp/fake /var/lib/hasler-apply
cp -r /repo/update/hasler_update /opt/hasler/updater/
install -m 0755 /repo/scripts/hasler-os-update /usr/local/sbin/hasler-os-update

say "vercmp agrees with dpkg"
python3 - <<'EOF'
import importlib.machinery, importlib.util, itertools, subprocess
l = importlib.machinery.SourceFileLoader("u", "/usr/local/sbin/hasler-os-update")
s = importlib.util.spec_from_loader("u", l); u = importlib.util.module_from_spec(s); l.exec_module(u)
vs = ["1.0", "1.0-1", "1.0-1+deb13u1", "1.0-1+deb13u10", "1.0-1+deb13u2", "1:0.9", "1.0~rc1", "1.0a", "1.0+b1",
      "2.30-1", "2.4-1", "0.24.4-1moode1", "0.24.4-1+deb13u1", "7:7.1.1-1+rpt1", "7:7.1.2-0+deb13u1", "1.0.0", "1.00",
      "3.5.1-1~deb13u1", "2025b-4", "2025b-0+deb13u1", "1.2.3-4.1", "1.2.3-4.1~bpo13+1"]
bad = []
for a, b in itertools.combinations(vs, 2):
    mine = u.vercmp(a, b); mine = (mine > 0) - (mine < 0)
    real = 0 if subprocess.run(["dpkg", "--compare-versions", a, "eq", b]).returncode == 0 else \
        (1 if subprocess.run(["dpkg", "--compare-versions", a, "gt", b]).returncode == 0 else -1)
    if mine != real:
        bad.append((a, b, mine, real))
print("mismatches:", bad)
assert not bad
EOF

say "test packages: installed at trixie's version while trixie-security has a newer one"
PKGS="$(python3 - <<'EOF'
import glob, subprocess
names = []
for f in glob.glob("/var/lib/apt/lists/*security*_Packages*"):
    text = subprocess.run(["/usr/lib/apt/apt-helper", "cat-file", f], capture_output=True, text=True).stdout
    names += [l.split(": ", 1)[1] for l in text.splitlines() if l.startswith("Package: ")]
good = []
for p in sorted(set(names)):
    if p.startswith(("linux-", "firmware-")) or p.endswith(("-dev", "-doc", "-dbg")):
        continue
    mad = subprocess.run(["apt-cache", "madison", p], capture_output=True, text=True).stdout
    main = [l.split("|")[1].strip() for l in mad.splitlines() if "trixie/main" in l and "security" not in l]
    if not main:
        continue
    sim = subprocess.run(["apt-get", "-s", "install", "--no-install-recommends", f"{p}={main[0]}"],
                         capture_output=True, text=True)
    if sim.returncode == 0 and 0 < sim.stdout.count("\nInst ") <= 2 and "Remv" not in sim.stdout:
        good.append(p)
    if len(good) == 3:
        break
print(" ".join(good))
EOF
)"
echo "candidates: $PKGS"
read -r A B C <<<"$PKGS" || true
[[ -n "${A:-}" && -n "${B:-}" ]] || fail "need two packages with a security update (got '$PKGS')"
for p in $A $B ${C:-}; do
  OLD="$(apt-cache madison "$p" | grep -v security | awk -F'|' '$3 ~ /trixie\/main/ {gsub(/ /,"",$2); print $2; exit}')"
  apt-get install -y -qq --no-install-recommends --allow-downgrades "$p=$OLD" >/dev/null
  echo "$p installed at $(ver "$p")"
done
A_OLD="$(ver "$A")"; B_OLD="$(ver "$B")"
apt-mark hold "$B" >/dev/null

say "fake haslerd /api/health"
cat > /tmp/fake/health.py <<EOF
import http.server, json, os, subprocess
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        broke = os.path.exists("/tmp/fake/break") and \
            subprocess.run(["dpkg-query", "-W", "-f", "\${Version}", "$A"], capture_output=True, text=True).stdout != "$A_OLD"
        doc = {"version": "1.0.3", "ok": not broke, "idle": os.path.exists("/tmp/fake/idle"), "standby": True,
               "playing": False, "checks": {"mpd": not broke, "audio_card": True, "panel": "ok", "lamp": True, "web": True}}
        body = json.dumps(doc).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
http.server.ThreadingHTTPServer(("127.0.0.1", 8080), H).serve_forever()
EOF
python3 /tmp/fake/health.py & sleep 1

say "1. radio in use -> deferred, nothing fetched"
rm -f /tmp/fake/idle
run --now
[[ $RC == 75 ]] || fail "not idle: rc $RC"
[[ ! -d /var/lib/hasler-os/lists ]] || [[ -z "$(ls /var/lib/hasler-os/lists 2>/dev/null | grep Packages)" ]] || fail "fetched while in use"
touch /tmp/fake/idle

say "2. no network -> deferred, nothing changed"
echo 'Acquire::http::Proxy "http://127.0.0.1:9/";' > /etc/apt/apt.conf.d/99no-network
run --now
rm /etc/apt/apt.conf.d/99no-network
[[ $RC == 75 ]] || fail "no network: rc $RC"
[[ "$(ver "$A")" == "$A_OLD" ]] || fail "changed without network"
st 'deferred["why"]'

say "3. dry run: $A planned, $B held"
run --now --dry-run
[[ $RC == 0 ]] || fail "dry run rc $RC"
st 'held_back'
st 'held_back' | grep -q "$B (held)" || fail "$B not reported held"
[[ "$(ver "$A")" == "$A_OLD" ]] || fail "dry run changed $A"

say "4. an upgrade that breaks the health check -> back to the old versions, paused"
touch /tmp/fake/break
run --now
[[ $RC == 1 ]] || fail "broken health: rc $RC"
[[ "$(ver "$A")" == "$A_OLD" ]] || fail "$A not put back (is $(ver "$A"), was $A_OLD)"
[[ "$(ver "$B")" == "$B_OLD" ]] || fail "held $B changed"
st 'last_result' | grep -qx rolled_back || fail "result not rolled_back"
st 'paused["until"]'
tail -1 /var/lib/hasler-os/events.jsonl
dpkg --audit | grep . && fail "dpkg left something half-done"
ls /var/lib/hasler-os/rollback/
run --now
[[ $RC == 75 ]] || fail "not paused: rc $RC"

say "5. a good night (fresh status: the failed versions are remembered otherwise)"
rm -f /tmp/fake/break /var/lib/hasler-os/status.json
run --now
[[ $RC == 0 ]] || fail "good night: rc $RC"
[[ "$(ver "$A")" != "$A_OLD" ]] || fail "$A not upgraded"
[[ "$(ver "$B")" == "$B_OLD" ]] || fail "held $B upgraded"
st 'upgraded'
st 'last_upgrade'
[[ -z "$(ls /var/cache/hasler-os/archives/*.deb 2>/dev/null)" ]] || fail "apt-get clean not done"
ls /var/lib/hasler-os/pre-*.list >/dev/null || fail "no snapshot"
[[ ! -e /run/hasler-os-update/active ]] || fail "lamp signal left behind"

say "6. once a night: the timer's second slot does nothing"
run --auto --now
[[ $RC == 0 ]] || fail "second slot rc $RC"

say "7. the way back by hand"
/usr/bin/python3 -I /usr/local/sbin/hasler-os-update rollback
[[ "$(ver "$A")" == "$A_OLD" ]] || fail "rollback by hand: $A is $(ver "$A")"
dpkg --audit | grep . && fail "dpkg left something half-done"

say "8. off in box.yaml"
mkdir -p /etc/hasler; printf 'schema: 1\nos_updates: off\n' > /etc/hasler/box.yaml
run --now
st 'last_result' | grep -qx off || fail "not off"

say "ALL PASSED"
