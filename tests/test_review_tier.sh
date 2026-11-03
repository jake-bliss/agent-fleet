#!/usr/bin/env bash
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
TIER="$REPO/bin/review-tier"
GATE="$REPO/bin/prepush-gate"
RECEIPT_SH="$REPO/bin/review-receipt"
PASS=0
FAIL=0
ok() { PASS=$((PASS + 1)); }
bad() { FAIL=$((FAIL + 1)); echo "FAIL: $*"; }
eq() { if [ "$2" = "$3" ]; then ok; else bad "$1: want '$3' got '$2'"; fi; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
export HOME="$TMP/home"; mkdir -p "$HOME"

git init -q --bare "$TMP/origin.git" -b main
mkrepo() {
  local d="$TMP/$1"
  git clone -q "$TMP/origin.git" "$d" 2>/dev/null
  git -C "$d" checkout -q -B main
  [ -f "$d/README" ] || { echo hi > "$d/README"; git -C "$d" add README; git -C "$d" commit -qm base; git -C "$d" push -q origin main; }
  git -C "$d" remote set-head origin main
  git -C "$d" checkout -q -b "$2"
  echo "$d"
}
lines() { seq 1 "$1" | sed 's/^/line /'; }

D=$(mkrepo small b-small)
mkdir -p "$D/lib"; lines 10 > "$D/lib/util.rb"; git -C "$D" add -A; git -C "$D" commit -qm small
eq "small safe diff" "$("$TIER" "$D" | head -1)" low

D=$(mkrepo big b-big)
mkdir -p "$D/lib"; lines 401 > "$D/lib/util.rb"; git -C "$D" add -A; git -C "$D" commit -qm big
eq "401 lines" "$("$TIER" "$D" | head -1)" high
lines 400 > "$D/lib/util.rb"; git -C "$D" commit -qam fewer
eq "400 lines" "$("$TIER" "$D" | head -1)" low

D=$(mkrepo jobs b-jobs)
mkdir -p "$D/app/jobs"; echo x > "$D/app/jobs/foo_job.rb"; git -C "$D" add -A; git -C "$D" commit -qm jobs
eq "app/jobs path" "$("$TIER" "$D" | head -1)" high
eq "reason line" "$("$TIER" "$D" | tail -1)" "risky path: app/jobs/foo_job.rb"
eq "explicit range" "$(cd "$D" && "$TIER" "main..b-jobs" | head -1)" high

for p in db/migrate/1_x.rb app/models/payment.rb lib/auth_helper.rb app/x/tenant_scope.rb; do
  D=$(mkrepo "p$RANDOM" b-p)
  mkdir -p "$D/$(dirname "$p")"; echo x > "$D/$p"; git -C "$D" add -A; git -C "$D" commit -qm p
  eq "risky $p" "$("$TIER" "$D" | head -1)" high
done

setup_gate() {
  local d="$1" sha; sha=$(git -C "$d" rev-parse HEAD)
  local gd; gd=$(git -C "$d" rev-parse --absolute-git-dir)
  echo "{\"sha\":\"$sha\",\"repo\":\"r\",\"ci\":{\"status\":\"green\"}}" > "$gd/adw-receipt.json"
  echo "[{\"who\":\"codex\",\"verdict\":\"pass\",\"sha\":\"$sha\"},{\"who\":\"claude\",\"verdict\":\"skip\",\"sha\":\"$sha\"}]" > "$gd/adw-review-history.json"
  echo "$sha"
}
run_gate() { ( cd "$1" && REVIEW_TIER="$TIER" bash "$GATE" <<<"refs/heads/x $2 refs/heads/x 0000000000000000000000000000000000000000" ) 2>&1; }

D=$(mkrepo glow g-low)
echo ok > "$D/note.txt"; git -C "$D" add -A; git -C "$D" commit -qm low
sha=$(setup_gate "$D")
out=$(run_gate "$D" "$sha"); rc=$?
eq "gate accepts claude skip on low tier" "$rc" 0

D=$(mkrepo ghigh g-high)
mkdir -p "$D/app/jobs"; echo x > "$D/app/jobs/a_job.rb"; git -C "$D" add -A; git -C "$D" commit -qm high
sha=$(setup_gate "$D")
out=$(run_gate "$D" "$sha"); rc=$?
eq "gate rejects claude skip on high tier" "$rc" 1
case "$out" in *"not low tier"*) ok ;; *) bad "gate rejection message: $out" ;; esac

gd=$(git -C "$D" rev-parse --absolute-git-dir)
python3 - "$gd/adw-review-history.json" "$sha" <<'PY'
import json, sys
h = json.load(open(sys.argv[1])); h.append({"who": "claude", "verdict": "pass", "sha": sys.argv[2]})
json.dump(h, open(sys.argv[1], "w"))
PY
run_gate "$D" "$sha" >/dev/null; eq "high tier with a claude pass still clears" "$?" 0

D=$(mkrepo rlow r-low)
echo ok > "$D/note.txt"; git -C "$D" add -A; git -C "$D" commit -qm low
( cd "$D" && REVIEW_TIER="$TIER" bash "$RECEIPT_SH" claude skip "low tier" >/dev/null 2>&1 ); eq "receipt skip on low" "$?" 0
D=$(mkrepo rhigh r-high)
mkdir -p "$D/app/jobs"; echo x > "$D/app/jobs/a_job.rb"; git -C "$D" add -A; git -C "$D" commit -qm high
( cd "$D" && REVIEW_TIER="$TIER" bash "$RECEIPT_SH" claude skip "low tier" >/dev/null 2>&1 ); eq "receipt skip refused on high" "$?" 3

tier_of() {
  local d; d=$(mkrepo "t$RANDOM" b-t)
  mkdir -p "$d/$(dirname "$1")"; seq 1 "${2:-3}" > "$d/$1"; git -C "$d" add -A; git -C "$d" commit -qm t
  "$TIER" "$d" | head -1
}
eq "sidekiq job" "$(tier_of apps/rails/app/sidekiq/foo_job.rb)" high
eq "refunds_controller" "$(tier_of apps/rails/app/controllers/refunds_controller.rb)" high
eq "orders model" "$(tier_of apps/rails/app/models/order.rb)" high
eq "monorepo jobs prefix" "$(tier_of apps/svc/app/jobs/webhooks/x.rb)" high
eq "Gemfile.lock only, huge" "$(tier_of apps/rails/Gemfile.lock 900)" low
eq "author.rb" "$(tier_of app/models/author.rb)" low
eq "authors_controller.rb" "$(tier_of app/controllers/authors_controller.rb)" low
eq "authorization" "$(tier_of app/services/authorization_service.rb)" high
eq "oauth" "$(tier_of app/controllers/oauth_callbacks_controller.rb)" high
eq "suffix sync" "$(tier_of app/services/member_sync_service.rb)" high
eq "async" "$(tier_of app/javascript/async_loader.js)" low
eq "prepush-gate.sh" "$(tier_of bin/prepush-gate)" high
eq "plain controller" "$(tier_of app/controllers/widgets_controller.rb)" low
eq "block word is not lock" "$(tier_of app/models/blocker.rb)" low

d=$(mkrepo dirform b-dir); echo x > "$d/n.txt"; git -C "$d" add -A; git -C "$d" commit -qm n
mkdir -p "$TMP/work"
eq "dir argument containing .." "$(cd "$TMP/work" && "$TIER" "../$(basename "$d")" | head -1)" low

hist_set() {
  local gd; gd=$(git -C "$1" rev-parse --absolute-git-dir)
  echo "{\"sha\":\"$2\",\"repo\":\"r\",\"ci\":{\"status\":\"green\"}}" > "$gd/adw-receipt.json"
  echo "$3" > "$gd/adw-review-history.json"
}

D=$(mkrepo anc a-anc)
echo ok > "$D/note.txt"; git -C "$D" add -A; git -C "$D" commit -qm low
low_sha=$(git -C "$D" rev-parse HEAD)
mkdir -p "$D/app/jobs"; echo x > "$D/app/jobs/a_job.rb"; git -C "$D" add -A; git -C "$D" commit -qm risky
sha=$(git -C "$D" rev-parse HEAD)
hist_set "$D" "$sha" "[{\"who\":\"codex\",\"verdict\":\"pass\",\"sha\":\"$sha\"},{\"who\":\"claude\",\"verdict\":\"skip\",\"sha\":\"$low_sha\"}]"
run_gate "$D" "$sha" >/dev/null; eq "skip on low ancestor, risky commit on top: refused" "$?" 1

D=$(mkrepo fo f-over)
echo ok > "$D/note.txt"; git -C "$D" add -A; git -C "$D" commit -qm low
sha=$(git -C "$D" rev-parse HEAD)
hist_set "$D" "$sha" "[{\"who\":\"codex\",\"verdict\":\"pass\",\"sha\":\"$sha\"},{\"who\":\"claude\",\"verdict\":\"fail\",\"sha\":\"$sha\"},{\"who\":\"claude\",\"verdict\":\"skip\",\"sha\":\"$sha\"}]"
run_gate "$D" "$sha" >/dev/null; eq "claude fail overrides later skip" "$?" 1

D="$TMP/nomb"; git init -q "$D" -b main; echo a > "$D/a"; git -C "$D" add -A; git -C "$D" commit -qm a
sha=$(git -C "$D" rev-parse HEAD)
hist_set "$D" "$sha" "[{\"who\":\"codex\",\"verdict\":\"pass\",\"sha\":\"$sha\"},{\"who\":\"claude\",\"verdict\":\"skip\",\"sha\":\"$sha\"}]"
out=$(run_gate "$D" "$sha"); eq "gate: no merge base refuses skip" "$?" 1
case "$out" in *"no merge base"*) ok ;; *) bad "no merge base message: $out" ;; esac
( cd "$D" && REVIEW_TIER="$TIER" bash "$RECEIPT_SH" claude skip "low tier" >/dev/null 2>&1 ); eq "receipt: no merge base exits 2" "$?" 2

echo '{"at":1,"who":"claude","verdict":"skip"}' > "$TMP/home/ledger.jsonl"
echo '{"at":4102444800,"who":"claude","verdict":"pass","found":1}' >> "$TMP/home/ledger.jsonl"
sed "s#^LEDGER=.*#LEDGER=$TMP/home/ledger.jsonl#" "$RECEIPT_SH" > "$TMP/rr.sh"
case "$(bash "$TMP/rr.sh" --ledger 100000 2>&1)" in *"verdicts=1 "*"skips=1"*) ok ;; *) bad "ledger skip bucket" ;; esac

echo "review-tier tests: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
