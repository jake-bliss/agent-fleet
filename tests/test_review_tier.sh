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
export HOME="$TMP/home"; mkdir -p "$HOME"; unset ADW_HOME

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

D=$(mkrepo fg b-flag)
echo ok > "$D/note.txt"; git -C "$D" add -A; git -C "$D" commit -qm flagged
eq "flag-free small diff is low" "$("$TIER" "$D" | head -1)" low
out=$("$TIER" --flagged new_widget "$D"); eq "--flagged small diff: flag-check" "$(head -1 <<<"$out")" flag-check
case "$out" in *new_widget*) ok ;; *) bad "flag reason: $out" ;; esac
eq "flag persists for later runs" "$("$TIER" "$D" | head -1)" flag-check
mb=$(git -C "$D" merge-base origin/main HEAD)
eq "flag persists for a range run in the checkout" "$(cd "$D" && "$TIER" "$mb..HEAD" | head -1)" flag-check
( cd "$D" && REVIEW_TIER="$TIER" bash "$RECEIPT_SH" claude skip "low tier" >/dev/null 2>&1 ); eq "receipt: skip refused when flag-gated" "$?" 3
sha=$(git -C "$D" rev-parse HEAD)
hist_set "$D" "$sha" "[{\"who\":\"codex\",\"verdict\":\"pass\",\"sha\":\"$sha\"},{\"who\":\"claude\",\"verdict\":\"skip\",\"sha\":\"$sha\"}]"
run_gate "$D" "$sha" >/dev/null; eq "gate: skip refused when flag-gated" "$?" 1
mkdir -p "$D/app/services/payments"; echo x > "$D/app/services/payments/charge.rb"; git -C "$D" add -A; git -C "$D" commit -qm risky
eq "flag-gated + risky path: high" "$("$TIER" "$D" | head -1)" high
lines 450 > "$D/big.txt"; git -C "$D" rm -q app/services/payments/charge.rb; git -C "$D" add -A; git -C "$D" commit -qm big
eq "flag-gated + big diff: high" "$("$TIER" "$D" | head -1)" high

write_report() {
  local d=$1 verdict=$2; shift 2
  ( cd "$d" && echo "{\"reviewer\":\"claude\",\"verdict\":\"$verdict\",\"findings\":[]}" | bash "$RECEIPT_SH" --write-report claude "$@" - 2>/dev/null ) | sed -n 's/^verdict file: //p'
}
gate_head() {
  local d=$1 sha gd; sha=$(git -C "$d" rev-parse HEAD); gd=$(git -C "$d" rev-parse --absolute-git-dir)
  echo "{\"sha\":\"$sha\",\"repo\":\"r\",\"ci\":{\"status\":\"green\"}}" > "$gd/adw-receipt.json"
  python3 -c 'import json,sys; p=sys.argv[1]; h=json.load(open(p)) if __import__("os").path.exists(p) else []; h.append({"who":"codex","verdict":"pass","sha":sys.argv[2]}); json.dump(h,open(p,"w"))' "$gd/adw-review-history.json" "$sha"
  run_gate "$d" "$sha"
}
D=$(mkrepo fexact b-fexact)
echo ok > "$D/note.txt"; git -C "$D" add -A; git -C "$D" commit -qm r1
"$TIER" --flagged new_widget "$D" >/dev/null
R1=$(git -C "$D" rev-parse HEAD)
VF=$(write_report "$D" pass --mode flag-check)
eq "write-report records flag-check mode" "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("mode"))' "$VF")" flag-check
( cd "$D" && REVIEW_TIER="$TIER" bash "$RECEIPT_SH" claude pass "fc" --report "$VF" >/dev/null 2>&1 ); eq "receipt: flag-check pass on HEAD records" "$?" 0
GD=$(git -C "$D" rev-parse --absolute-git-dir)
eq "history row carries the mode" "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))[-1].get("mode"))' "$GD/adw-review-history.json")" flag-check
gate_head "$D" >/dev/null; eq "gate: flag-check pass clears its own commit" "$?" 0
echo more >> "$D/note.txt"; git -C "$D" commit -qam fix1
out=$(gate_head "$D"); eq "gate: flag-check pass does not carry to a fix commit" "$?" 1
case "$out" in *"no passing claude review"*) ok ;; *) bad "carry refusal message: $out" ;; esac
( cd "$D" && REVIEW_TIER="$TIER" bash "$RECEIPT_SH" claude pass "fc" --report "$VF" >/dev/null 2>&1 ); eq "receipt: old flag-check report refused on a new HEAD" "$?" 3
FAILVF=$(cd "$D" && echo '{"reviewer":"claude","verdict":"fail","findings":[{"severity":"important","confidence":"high","claim":"x"}]}' | bash "$RECEIPT_SH" --write-report claude --sha "$R1" --mode flag-check - 2>/dev/null | sed -n 's/^verdict file: //p')
echo "{\"kind\":\"adw-review-verdict\",\"who\":\"codex\",\"verdict\":\"pass\",\"sha\":\"$(git -C "$D" rev-parse HEAD)\",\"since\":\"$R1\",\"findings\":[]}" > "$TMP/codex-delta.json"
( cd "$D" && REVIEW_TIER="$TIER" bash "$RECEIPT_SH" claude pass "carry" --report "$FAILVF" --fixed-by "$TMP/codex-delta.json" >/dev/null 2>&1 ); eq "receipt: codex delta cannot carry a flag-check fail" "$?" 3
VF2=$(write_report "$D" pass --mode flag-check)
( cd "$D" && REVIEW_TIER="$TIER" bash "$RECEIPT_SH" claude pass "fc2" --report "$VF2" >/dev/null 2>&1 ); eq "receipt: fresh flag-check pass on the fix" "$?" 0
gate_head "$D" >/dev/null; eq "gate: fresh flag-check pass clears the fix commit" "$?" 0

mkdir -p "$D/app/services/payments"; echo x > "$D/app/services/payments/charge.rb"; git -C "$D" add -A; git -C "$D" commit -qm risky
VF3=$(write_report "$D" pass --mode flag-check)
( cd "$D" && REVIEW_TIER="$TIER" bash "$RECEIPT_SH" claude pass "fc3" --report "$VF3" >/dev/null 2>&1 ); eq "receipt: flag-check pass refused on a high diff" "$?" 3
H=$(git -C "$D" rev-parse HEAD)
python3 -c 'import json,sys; p=sys.argv[1]; h=json.load(open(p)); h.append({"who":"claude","verdict":"pass","sha":sys.argv[2],"mode":"flag-check"}); json.dump(h,open(p,"w"))' "$GD/adw-review-history.json" "$H"
out=$(gate_head "$D"); eq "gate: exact-head flag-check pass refused on a high diff" "$?" 1
case "$out" in *"no passing claude review"*) ok ;; *) bad "high-diff refusal message: $out" ;; esac
out=$( cd "$D" && REVIEW_TIER=/usr/bin/false bash "$GATE" <<<"refs/heads/x $H refs/heads/x 0000000000000000000000000000000000000000" 2>&1 ); eq "gate: flag-check tier check fails closed" "$?" 1

( cd "$D" && echo '{"reviewer":"claude","verdict":"pass","findings":[]}' | bash "$RECEIPT_SH" --write-report claude - >/dev/null 2>&1 ); eq "write-report: flag-gated checkout must name --mode" "$?" 2
( cd "$D" && echo '{"reviewer":"codex","verdict":"pass","findings":[]}' | bash "$RECEIPT_SH" --write-report codex - >/dev/null 2>&1 ); eq "write-report: codex needs no mode" "$?" 0

D=$(mkrepo ffull b-ffull)
echo ok > "$D/note.txt"; git -C "$D" add -A; git -C "$D" commit -qm r1
VF=$(write_report "$D" pass)
eq "full mode leaves no mode field" "$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("mode"))' "$VF")" None
( cd "$D" && REVIEW_TIER="$TIER" bash "$RECEIPT_SH" claude pass "full" --report "$VF" >/dev/null 2>&1 )
echo more >> "$D/note.txt"; git -C "$D" commit -qam fix1
gate_head "$D" >/dev/null; eq "gate: a full pass still carries to a fix commit" "$?" 0

D=$(mkrepo fhigh b-fhigh)
mkdir -p "$D/app/services/payments"; echo x > "$D/app/services/payments/x.rb"; git -C "$D" add -A; git -C "$D" commit -qm risky
"$TIER" --flagged new_widget "$D" >/dev/null
GD=$(git -C "$D" rev-parse --absolute-git-dir)
RH1=$(git -C "$D" rev-parse HEAD)
eq "flagged repo with risky path is high" "$("$TIER" "$D" | head -1)" high
VH0=$(write_report "$D" pass --mode flag-check)
( cd "$D" && REVIEW_TIER="$TIER" bash "$RECEIPT_SH" claude pass "fc" --report "$VH0" >/dev/null 2>&1 ); eq "receipt: high flag-check pass refused with no full pass" "$?" 3
gate_head "$D" >/dev/null; eq "gate: high diff with no claude row refused" "$?" 1
HFAIL=$(cd "$D" && echo '{"reviewer":"claude","verdict":"fail","findings":[{"severity":"important","confidence":"high","claim":"x"}]}' | bash "$RECEIPT_SH" --write-report claude --sha "$RH1" --mode full - 2>/dev/null | sed -n 's/^verdict file: //p')
echo more >> "$D/app/services/payments/x.rb"; git -C "$D" commit -qam fix
RH2=$(git -C "$D" rev-parse HEAD)
echo "{\"kind\":\"adw-review-verdict\",\"who\":\"codex\",\"verdict\":\"pass\",\"sha\":\"$RH2\",\"since\":\"$RH1\",\"findings\":[]}" > "$TMP/codex-delta-h.json"
( cd "$D" && REVIEW_TIER="$TIER" bash "$RECEIPT_SH" claude pass "carry" --report "$HFAIL" --fixed-by "$TMP/codex-delta-h.json" >/dev/null 2>&1 ); eq "receipt: full fail carried via codex delta" "$?" 0
VH1=$(write_report "$D" pass --mode flag-check)
( cd "$D" && REVIEW_TIER="$TIER" bash "$RECEIPT_SH" claude pass "fc" --report "$VH1" >/dev/null 2>&1 ); eq "receipt: high flag-check confirmation records after a carried full pass" "$?" 0
eq "confirmation row carries confirms" "$(python3 -c 'import json,sys;r=json.load(open(sys.argv[1]))[-1];print(r.get("mode"),r.get("confirms"))' "$GD/adw-review-history.json")" "flag-check flag-coverage"
eq "ledger row carries confirms" "$(tail -1 "$HOME/.claude/adw/review-ledger.jsonl" 2>/dev/null | python3 -c 'import json,sys;print(json.load(sys.stdin).get("confirms"))' 2>/dev/null)" flag-coverage
gate_head "$D" >/dev/null; eq "gate: high diff clears on the carried full row" "$?" 0
python3 -c 'import json,sys; p=sys.argv[1]; h=[r for r in json.load(open(p)) if r.get("who")!="claude" or r.get("mode")=="flag-check"]; json.dump(h,open(p,"w"))' "$GD/adw-review-history.json"
gate_head "$D" >/dev/null; eq "gate: high diff with only a flag-check confirmation refused" "$?" 1

D=$(mkrepo fhunv b-fhunv)
mkdir -p "$D/app/services/payments"; echo x > "$D/app/services/payments/x.rb"; git -C "$D" add -A; git -C "$D" commit -qm risky
"$TIER" --flagged new_widget "$D" >/dev/null
( cd "$D" && REVIEW_TIER="$TIER" bash "$RECEIPT_SH" claude pass "no report" >/dev/null 2>&1 )
VU=$(write_report "$D" pass --mode flag-check)
( cd "$D" && REVIEW_TIER="$TIER" bash "$RECEIPT_SH" claude pass "fc" --report "$VU" >/dev/null 2>&1 ); eq "receipt: high flag-check refused when the only full pass is unverified" "$?" 3

echo "review-tier tests: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
