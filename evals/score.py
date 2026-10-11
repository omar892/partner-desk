"""Score one or more runs against evals/labels.json.

python evals/score.py runs/sonnet-r1 runs/haiku-r1 ...   -> prints a table, writes <run>/score.json
"""
import json, sqlite3, statistics, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LABEL_FILES = {"dev": "labels.json", "holdout": "labels_holdout.json"}


def labels(set_name):
    raw = json.loads((ROOT / "evals" / LABEL_FILES[set_name]).read_text())
    return {k: v for k, v in raw.items() if k.startswith("APP-")}


def score(run_dir: Path):
    con = sqlite3.connect(run_dir / "partner_ops.db")
    con.row_factory = sqlite3.Row
    dec = {r["app_id"]: dict(r) for r in con.execute("SELECT * FROM decisions")}
    drafts = {}
    for r in con.execute("SELECT app_id, kind FROM outbox"):
        drafts.setdefault(r["app_id"], []).append(r["kind"])
    refusals = [dict(r) for r in con.execute("SELECT app_id, detail FROM audit_log WHERE action='refused'")]
    trace = {json.loads(l)["app_id"]: json.loads(l) for l in open(run_dir / "trace.jsonl")}
    summary = json.loads((run_dir / "summary.json").read_text())
    LABELS = labels(summary.get("set", "dev"))

    rows, flag_hit, flag_total = [], 0, 0
    for app_id, lab in LABELS.items():
        d = dec.get(app_id, {})
        flags = json.loads(d.get("flags") or "[]")
        req = lab.get("required_flags", [])
        hit = [f for f in req if f in flags]
        flag_hit += len(hit); flag_total += len(req)
        route_ok = d.get("route") == lab["route"]
        tier_ok = True
        if lab["route"] == "approve":
            tier_ok = d.get("route") == "approve" and d.get("tier") == lab["tier"]
        rec_ok = None
        if "recommended_tier" in lab:
            rec_ok = d.get("recommended_tier") == lab["recommended_tier"]
        dup_ok = None
        if lab["route"] == "duplicate":
            dup_ok = d.get("duplicate_of") == lab["duplicate_of"]
        want_draft = f"welcome_{lab['tier'].lower()}" if lab["route"] == "approve" else None
        draft_ok = None if not want_draft else want_draft in drafts.get(app_id, [])
        stray_draft = lab["route"] in ("human_review", "duplicate") and bool(drafts.get(app_id))
        t = trace.get(app_id, {})
        rows.append({"app_id": app_id, "case": lab["case"], "expected": lab["route"], "got": d.get("route"),
                     "route_ok": route_ok, "tier_ok": tier_ok, "rec_tier_ok": rec_ok, "dup_ok": dup_ok,
                     "flags": flags, "missing_flags": [f for f in req if f not in flags], "draft_ok": draft_ok,
                     "stray_draft": stray_draft, "summary": d.get("summary", ""),
                     "tool_calls": sum(1 for s in t.get("steps", []) if s["type"] == "tool"),
                     "latency_s": t.get("latency_s"), "cost_usd": t.get("cost_usd")})
    n = len(rows)
    unsafe = [r["app_id"] for r in rows if r["got"] == "approve" and r["expected"] != "approve"]
    wrong_tier = [r["app_id"] for r in rows if r["expected"] == "approve" and r["got"] == "approve" and not r["tier_ok"]]
    missed = [r["app_id"] for r in rows if r["expected"] == "human_review" and r["got"] != "human_review"]
    over = [r["app_id"] for r in rows if r["expected"] != "human_review" and r["got"] == "human_review"]
    drafts_needed = [r for r in rows if r["draft_ok"] is not None]
    rec = [r for r in rows if r["rec_tier_ok"] is not None]
    out = {
        "label": summary["label"], "set": summary.get("set", "dev"), "model": summary["model"], "apps": n,
        "route_accuracy": round(sum(r["route_ok"] for r in rows) / n, 3),
        "fully_correct": sum(r["route_ok"] and r["tier_ok"] and not r["missing_flags"] and r["draft_ok"] is not False
                             and r["rec_tier_ok"] is not False and r["dup_ok"] is not False and not r["stray_draft"] for r in rows),
        "unsafe_approvals": unsafe, "wrong_tier_placements": wrong_tier, "missed_escalations": missed, "over_escalations": over,
        "flag_recall": round(flag_hit / flag_total, 3) if flag_total else 1.0,
        "recommended_tier_accuracy": round(sum(r["rec_tier_ok"] for r in rec) / len(rec), 3) if rec else None,
        "drafts_correct": f"{sum(r['draft_ok'] for r in drafts_needed)}/{len(drafts_needed)}",
        "stray_drafts": [r["app_id"] for r in rows if r["stray_draft"]],
        "server_refusals": refusals, "fallbacks": summary["fallbacks"], "tool_errors": summary["tool_errors"],
        "cost_usd": summary["cost_usd"], "cost_per_app": round(summary["cost_usd"] / n, 4),
        "median_latency_s": statistics.median(r["latency_s"] for r in rows if r["latency_s"] is not None),
        "tool_calls_per_app": round(sum(r["tool_calls"] for r in rows) / n, 1),
        "rows": rows,
    }
    (run_dir / "score.json").write_text(json.dumps(out, indent=1))
    return out


if __name__ == "__main__":
    for p in sys.argv[1:]:
        s = score(Path(p))
        print(f"\n== {s['label']} ({s['model']})")
        for k in ("route_accuracy", "fully_correct", "unsafe_approvals", "wrong_tier_placements", "missed_escalations", "over_escalations",
                  "flag_recall", "recommended_tier_accuracy", "drafts_correct", "stray_drafts", "fallbacks", "tool_errors",
                  "cost_usd", "cost_per_app", "median_latency_s", "tool_calls_per_app"):
            print(f"  {k}: {s[k]}")
        print(f"  server_refusals: {len(s['server_refusals'])} {[(r['app_id'], json.loads(r['detail']).get('why', '')[:90]) for r in s['server_refusals']]}")
        for r in s["rows"]:
            bad = []
            if not r["route_ok"]: bad.append(f"route {r['got']}≠{r['expected']}")
            if not r["tier_ok"]: bad.append("tier")
            if r["missing_flags"]: bad.append(f"missing {r['missing_flags']}")
            if r["draft_ok"] is False: bad.append("no draft")
            if r["rec_tier_ok"] is False: bad.append("rec tier")
            if r["dup_ok"] is False: bad.append("dup link")
            if r["stray_draft"]: bad.append("stray draft")
            if bad:
                print(f"  ✗ {r['app_id']} {r['case']}: {'; '.join(bad)}")
