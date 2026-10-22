#!/usr/bin/env python3
"""Per-epic delivery metrics: duration, held time, segments, PR cycle time, review-bot rounds, user decisions, spend.

Usage:
  epic-report.py <epic>              # print the report (live epics too)
  epic-report.py <epic> --write      # also save epics/<epic>/REPORT.json + upsert $ADW_HOME/efficiency/epics.jsonl
  epic-report.py --backfill          # --write every COMPLETE epic and every all-segments-done one (end = last merge)
  epic-report.py --history           # one line per recorded epic, newest first
"""
import argparse, concurrent.futures, glob, importlib.machinery, importlib.util, json, os, re, shutil, statistics, subprocess, sys, time
from pathlib import Path

ADW = Path(os.environ.get("ADW_HOME") or Path.home() / ".claude/adw")
EPICS = ADW / "epics"
ASKS = ADW / "asks"
HISTORY = ADW / "efficiency/epics.jsonl"
PROJECTS = Path.home() / ".claude/projects"
DONE = {"merged", "merged-deployed", "done-direct-prod", "executed"}
DROPPED = {"dropped", "superseded", "cancelled", "cancelled-replanned", "invalid", "closed-wont-do", "closed-no-defect",
           "reclassified", "resolved-by-measurement", "decomposed"}


def load_efficiency_report():
    for path in (os.environ.get("EFFICIENCY_REPORT"), str(Path(__file__).resolve().parent.parent / "bin/efficiency-report"),
                 shutil.which("efficiency-report")):
        if path and os.path.isfile(path):
            loader = importlib.machinery.SourceFileLoader("effrep", path)
            spec = importlib.util.spec_from_loader("effrep", loader)
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
            return module
    sys.exit("epic-report: efficiency-report not found (set EFFICIENCY_REPORT)")


effrep = load_efficiency_report()


def config():
    try:
        return json.loads((ADW / "pr-tools.json").read_text())
    except (OSError, ValueError):
        return {}


REVIEW_PREFIX = config().get("review_prefix") or "Review bot:"


def manifest_slug(repo_key):
    try:
        repos = json.loads((ADW / "manifest.json").read_text())["repos"]
        path = os.path.expanduser(repos[repo_key]["path"])
        url = subprocess.check_output(["git", "-C", path, "remote", "get-url", "origin"], text=True,
                                      stderr=subprocess.DEVNULL).strip()
        return url.split("github.com")[-1].lstrip(":/").removesuffix(".git")
    except Exception:
        return None


def gh_repo(seg_repo, epic_repo):
    r = (seg_repo or epic_repo or "").strip()
    if re.fullmatch(r"[\w.-]+/[\w.-]+", r):
        return r
    return manifest_slug(r) or manifest_slug(epic_repo or "")


def pr_numbers(pr):
    if isinstance(pr, int):
        return [pr]
    if isinstance(pr, list):
        return [n for p in pr for n in pr_numbers(p)]
    s = str(pr or "").strip()
    if re.fullmatch(r"#?\d+", s):
        return [int(s.lstrip("#"))]
    return [int(n) for n in re.findall(r"(?:#|/pull/)(\d+)", s)]


def iso_ts(s):
    return time.mktime(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone if s else None


def pr_facts(repo, number):
    proc = subprocess.run(["gh", "pr", "view", str(number), "-R", repo, "--json",
                           "createdAt,mergedAt,state,additions,deletions,commits,reviews,author"],
                          capture_output=True, text=True, timeout=60)
    if proc.returncode:
        return None
    d = json.loads(proc.stdout)
    fp = {(r.get("commit") or {}).get("oid") for r in d.get("reviews") or []
          if (r.get("body") or "").startswith(REVIEW_PREFIX)}
    return {"repo": repo, "number": number, "state": d["state"], "author": (d.get("author") or {}).get("login"),
            "created": iso_ts(d["createdAt"]), "merged": iso_ts(d.get("mergedAt")),
            "lines": d["additions"] + d["deletions"], "commits": len(d.get("commits") or []), "fp_rounds": len(fp - {None})}


def held_seconds(d, start, end):
    total, opened = 0, None
    log = d / "HOLDS.jsonl"
    for line in log.read_text().splitlines() if log.exists() else []:
        ev = json.loads(line)
        if ev["event"] == "pause" and opened is None:
            opened = ev["at"]
        elif ev["event"] == "resume" and opened is not None:
            total += max(0, min(ev["at"], end) - max(opened, start)); opened = None
    paused = d / "PAUSED.json"
    if opened is None and paused.exists():
        opened = json.loads(paused.read_text()).get("paused_at")
    if opened is not None:
        total += max(0, end - max(opened, start))
    return total


def lead_sessions(slug, d, start):
    """Main transcripts that drove the epic: lead.json's session, or ones whose adw-lead-state/graph references are mostly
    this slug (>=5). Multi-epic tabs (the first mate) attribute to no single epic unless one dominates (>=60%)."""
    pat = re.compile(r"(?:adw-lead-state\s+[\w-]+\s+|epics/)([A-Za-z0-9][\w.-]*?)(?=[\s/\\\"'`]|$)")
    known = set()
    lj = d / "lead.json"
    if lj.exists():
        known.add(json.loads(lj.read_text()).get("session"))
    found = []
    for f in glob.glob(str(PROJECTS / "*/*.jsonl")):
        try:
            if os.path.getmtime(f) < start:
                continue
            if Path(f).stem in known:
                found.append(f); continue
            with open(f, errors="ignore") as fh:
                text = fh.read()
        except OSError:
            continue
        if slug not in text:
            continue
        counts = {}
        for m in pat.findall(text):
            counts[m] = counts.get(m, 0) + 1
        mine = counts.get(slug, 0)
        if mine >= 5 and mine >= 0.6 * sum(counts.values()):
            found.append(f)
    return found


def spend(files):
    cost, opus_sub, turns = 0.0, 0.0, 0
    for f in files:
        paths = [f] + glob.glob(f[:-6] + "/subagents/*.jsonl")
        for p in paths:
            seen = set()
            try:
                fh = open(p, errors="ignore")
            except OSError:
                continue
            with fh:
                for line in fh:
                    if '"assistant"' not in line:
                        continue
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    m = r.get("message") or {}
                    if r.get("type") != "assistant" or m.get("id") in seen or m.get("model") in (None, "<synthetic>"):
                        continue
                    seen.add(m.get("id"))
                    u = m.get("usage") or {}
                    i, cr, cw, o = (u.get(k, 0) or 0 for k in
                                    ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"))
                    pi, po, pcr, pcw = effrep.price(m["model"])
                    c = (i * pi + cr * pcr + cw * pcw + o * po) / 1e6
                    cost += c; turns += 1
                    if "/subagents/" in p and "opus" in m["model"]:
                        opus_sub += c
    return round(cost, 1), round(opus_sub, 1), turns


def oldest_transcript():
    times = [os.path.getmtime(f) for f in glob.glob(str(PROJECTS / "*/*.jsonl"))]
    return time.strftime("%Y-%m-%d", time.localtime(min(times))) if times else None


def pct(vals, q):
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(round(q * (len(vals) - 1))))] if vals else None


def epic_born(d, files):
    """Creation time of the epic. Linux has no st_birthtime and graph.json is rewritten on every update,
    so there the oldest mtime of any file in the epic directory stands in for it."""
    births = [x.stat().st_birthtime for x in files if x.exists() and hasattr(x.stat(), "st_birthtime")]
    if births:
        return min(births)
    return min(x.stat().st_mtime for x in d.rglob("*") if x.is_file())


def report(slug, with_spend=True):
    d = EPICS / slug
    g = json.loads((d / "graph.json").read_text())
    segs = g.get("segments") or []
    complete = json.loads((d / "COMPLETE.json").read_text()) if (d / "COMPLETE.json").exists() else None
    targets = sorted({(gh_repo(s.get("repo"), g.get("repo")), n) for s in segs if s.get("status") in DONE | {"pr-open", "pr-ready"}
                      for n in pr_numbers(s.get("pr")) if gh_repo(s.get("repo"), g.get("repo"))})
    with concurrent.futures.ThreadPoolExecutor(6) as pool:
        prs = [p for p in pool.map(lambda t: pr_facts(*t), targets) if p]
    merged = [p for p in prs if p["merged"]]
    plan = d / "plan.md"
    born = epic_born(d, (plan, d / "graph.json"))
    # PRs opened long before the plan were adopted, not built by the epic; they don't move its start.
    start = min([born] + [p["created"] for p in prs if p["created"] >= born - 14 * 86400])
    all_done = bool(segs) and all(s.get("status") in DONE | DROPPED for s in segs)
    end = complete["completed_at"] if complete else \
        max((p["merged"] for p in merged), default=(d / "graph.json").stat().st_mtime) if all_done else time.time()
    held = held_seconds(d, start, end)
    asks = [json.loads(p.read_text()) for p in ASKS.glob("d-*.json")]
    asks = [a for a in asks if a.get("epic") == slug]
    waits = [(a["answer"]["at"] - a["created"]) / 3600 for a in asks if isinstance(a.get("answer"), dict) and a["answer"].get("at")]
    cycle = [(p["merged"] - p["created"]) / 3600 for p in merged]
    statuses = {}
    for s in segs:
        statuses[s.get("status")] = statuses.get(s.get("status"), 0) + 1
    out = {
        "epic": slug, "repo": g.get("repo"), "goal": (g.get("goal") or "")[:200],
        "status": "complete" if complete else "done-unmarked" if all_done else
                  "paused" if (d / "PAUSED.json").exists() else "live",
        "started": time.strftime("%Y-%m-%d %H:%M", time.localtime(start)),
        "completed": time.strftime("%Y-%m-%d %H:%M", time.localtime(end)) if complete or all_done else None,
        "wall_days": round((end - start) / 86400, 1), "held_days": round(held / 86400, 1),
        "active_days": round((end - start - held) / 86400, 1),
        "segments": len(segs), "segments_done": sum(s.get("status") in DONE for s in segs),
        "segments_dropped": sum(s.get("status") in DROPPED for s in segs), "segment_status": statuses,
        "prs_merged": len(merged), "lines_merged": sum(p["lines"] for p in merged),
        "median_pr_lines": pct([p["lines"] for p in merged], 0.5),
        "cycle_h_median": round(pct(cycle, 0.5), 1) if cycle else None,
        "cycle_h_p90": round(pct(cycle, 0.9), 1) if cycle else None,
        "fp_rounds_median": pct([p["fp_rounds"] for p in merged], 0.5),
        "fp_rounds_max": max((p["fp_rounds"] for p in merged), default=None),
        "fp_over_3": sum(p["fp_rounds"] > 3 for p in merged),
        "commits_per_pr_median": pct([p["commits"] for p in merged], 0.5),
        "user_decisions": len(asks), "decision_wait_h_median": round(statistics.median(waits), 1) if waits else None,
        "recorded_at": time.strftime("%Y-%m-%d %H:%M"),
    }
    out["prs_per_active_day"] = round(len(merged) / max(out["active_days"], 1), 1)
    oldest = oldest_transcript()
    if with_spend and oldest and time.strftime("%Y-%m-%d", time.localtime(start)) < oldest:
        out.update({"spend_usd": None, "usd_per_pr": None, "spend_note": f"started before transcripts kept ({oldest})"})
    elif with_spend:
        files = lead_sessions(slug, d, start - 86400)
        cost, opus_sub, turns = spend(files)
        if not files:
            cost = None
        out.update({"lead_session_ids": [Path(f).stem[:8] for f in files], "spend_usd": cost, "spend_opus_sub_usd": opus_sub, "spend_turns": turns, "lead_sessions": len(files),
                    "usd_per_pr": round(cost / len(merged), 1) if merged and cost is not None else None,
                    "spend_note": "lead sessions + their subagents, list-price; Codex usage not counted",
                    "transcripts_from": oldest_transcript()})
    return out


def show(r):
    print(f"{r['epic']} [{r['status']}] {r['repo']} · {r['started']} → {r['completed'] or 'now'}")
    print(f"  {r['wall_days']}d wall · {r['held_days']}d held · {r['active_days']}d active")
    print(f"  segments {r['segments_done']}/{r['segments']} done · {r['segments_dropped']} dropped · {r['segment_status']}")
    print(f"  PRs merged {r['prs_merged']} ({r['prs_per_active_day']}/active day) · {r['lines_merged']} lines "
          f"(median {r['median_pr_lines']}/PR) · cycle median {r['cycle_h_median']}h p90 {r['cycle_h_p90']}h")
    print(f"  Review-bot rounds median {r['fp_rounds_median']} max {r['fp_rounds_max']} ({r['fp_over_3']} PRs >3) · "
          f"commits/PR {r['commits_per_pr_median']}")
    print(f"  User decisions {r['user_decisions']} (median answer {r['decision_wait_h_median']}h)")
    if r.get("spend_usd") is not None:
        print(f"  spend ${r['spend_usd']} (${r['usd_per_pr']}/PR, Opus subagents ${r['spend_opus_sub_usd']}) over "
              f"{r['lead_sessions']} lead sessions · {r['spend_note']}; transcripts kept from {r['transcripts_from']}")


def write(r):
    (EPICS / r["epic"] / "REPORT.json").write_text(json.dumps(r, indent=1) + "\n")
    rows = [json.loads(l) for l in HISTORY.read_text().splitlines()] if HISTORY.exists() else []
    rows = [x for x in rows if x["epic"] != r["epic"]] + [r]
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text("".join(json.dumps(x) + "\n" for x in rows))


def history():
    rows = [json.loads(l) for l in HISTORY.read_text().splitlines()] if HISTORY.exists() else []
    print(f"{'epic':34s} {'done':10s} {'active_d':>8s} {'held_d':>6s} {'segs':>5s} {'PRs':>4s} {'PR/d':>5s} "
          f"{'cyc_h':>6s} {'fp_r':>4s} {'asks':>4s} {'$':>6s} {'$/PR':>5s}")
    for r in sorted(rows, key=lambda x: x.get("completed") or "9999", reverse=True):
        print(f"{r['epic'][:34]:34s} {(r.get('completed') or r['status'])[:10]:10s} {r['active_days']:8} {r['held_days']:6} "
              f"{r['segments']:5d} {r['prs_merged']:4d} {str(r['prs_per_active_day']):>5s} {str(r['cycle_h_median']):>6s} "
              f"{str(r['fp_rounds_median']):>4s} {r['user_decisions']:4d} {str(r.get('spend_usd', '-')):>6s} "
              f"{str(r.get('usd_per_pr', '-')):>5s}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("epic", nargs="?")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--history", action="store_true")
    ap.add_argument("--no-spend", action="store_true")
    a = ap.parse_args()
    if a.history:
        return history()
    if a.backfill:
        names = [p.parent.name for p in EPICS.glob("*/graph.json") if (p.parent / "COMPLETE.json").exists() or
                 (lambda segs: bool(segs) and all(x.get("status") in DONE | DROPPED for x in segs))(
                     json.loads(p.read_text()).get("segments") or [])]
    else:
        names = [a.epic]
    if not names or not names[0]:
        ap.error("epic name, --backfill or --history")
    for n in names:
        if not (EPICS / n / "graph.json").exists():
            sys.exit(f"no epic '{n}'")
        r = report(n, with_spend=not a.no_spend)
        show(r)
        if a.write or a.backfill:
            write(r)


if __name__ == "__main__":
    sys.exit(main())
