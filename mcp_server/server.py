"""partner-systems: a custom MCP server over the partner team's systems of record.

Reads: applications (portal), CRM partner + direct accounts, certification platform, Claude account data,
public story fetches, compliance screening. Writes: decisions, CRM partner accounts, review queue, outbox.

Guardrails live HERE, not only in the prompt. The server recomputes placement from its own data and
refuses any write the data doesn't support, so a confused or manipulated model can't place a partner
in the wrong tier, decline anyone, or email anyone but the applicant.

Run: PARTNER_DB=data/partner_ops.db python mcp_server/server.py   (stdio transport)
"""
import json, os, re, sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from mcp.server.mcpserver import MCPServer

ROOT = Path(__file__).resolve().parent.parent
DB = os.environ.get("PARTNER_DB", str(ROOT / "data" / "partner_ops.db"))
RUBRIC = json.loads((ROOT / "config" / "rubric.json").read_text())
TODAY = os.environ.get("PARTNER_TODAY", RUBRIC["as_of"])
ACTIVE_SINCE = (date.fromisoformat(TODAY) - timedelta(days=RUBRIC["practitioner_activity_days"])).isoformat()
TRAILING_SINCE = (date.fromisoformat(TODAY) - timedelta(days=RUBRIC["trailing_window_days"])).isoformat()
MAX_TIER = RUBRIC["agent_max_tier"]
RANK = {t: v["rank"] for t, v in RUBRIC["tiers"].items()}
ROUTES = {"approve", "human_review", "duplicate"}
FLAGS = {"bench_mismatch", "expired_certs", "inactive_practitioners", "non_domain_emails", "pilot_only",
         "customer_not_found", "attribution_conflict", "reference_unreachable", "reference_gated", "reference_missing",
         "duplicate_submission", "existing_partner", "channel_conflict", "compliance_review", "embedded_instructions",
         "tier_above_select", "upsell_signal", "likely_spam", "agent_incomplete"}
INJECTION = re.compile(r"(ai reviewer|ignore (all|previous|your)|skip (the )?(certification )?checks|record the approval|"
                       r"do not route to a human|pre-?approved)", re.I)

mcp = MCPServer("partner-systems")


def db():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def audit(con, action, app_id, detail, actor="agent"):
    con.execute("INSERT INTO audit_log(ts,actor,action,app_id,detail) VALUES(?,?,?,?,?)",
                (now(), actor, action, app_id, json.dumps(detail)[:4000]))


def mark(con, app_id, check, arg=""):
    con.execute("INSERT INTO checks_run VALUES(?,?,?,?)", (app_id, check, arg, now()))


def load_app(con, app_id):
    row = con.execute("SELECT payload,status FROM applications WHERE app_id=?", (app_id,)).fetchone()
    if not row:
        raise ValueError(f"No application {app_id}")
    a = json.loads(row["payload"])
    a["status"] = row["status"]
    return a


# ---------- shared verification logic (used by the tools AND by the write guard) ----------

def bench(con, domain, firm_name):
    dom = "%@" + domain.lower()
    on_dom = [dict(r) for r in con.execute("SELECT * FROM certifications WHERE lower(email) LIKE ?", (dom,))]
    other = [dict(r) for r in con.execute(
        "SELECT * FROM certifications WHERE lower(company_self_reported)=lower(?) AND lower(email) NOT LIKE ?", (firm_name, dom))]
    current = [p for p in on_dom if p["expires"] >= TODAY]
    active = [p for p in current if p["last_claude_use"] >= ACTIVE_SINCE]
    sample = [{k: p[k] for k in ("full_name", "email", "expires", "last_claude_use")} for p in (on_dom + other)[:10]]
    return {"domain": domain, "as_of": TODAY, "active_certified": len(active),
            "current_cert_but_no_claude_use_90d": len(current) - len(active),
            "expired_certs": len(on_dom) - len(current), "other_domain_matches": len(other),
            "records_found_any_status": len(on_dom) + len(other), "sample_people": sample}


def joint_customers(con, a):
    rows = []
    for c in a["customers"]:
        r = con.execute("SELECT * FROM claude_accounts WHERE lower(customer_name)=lower(?)", (c["customer"],)).fetchone()
        if not r:
            st = "not_found"
        elif r["partner_of_record"] and r["partner_of_record"].lower() != a["domain"].lower():
            st = "attributed_to_other_partner"
        elif not r["production_since"]:
            st = "no_production_usage"
        elif (r["last_production_use"] or "") < TRAILING_SINCE:
            st = "production_outside_trailing_12_months"
        else:
            st = "verified"
        rows.append({"customer": c["customer"], "status": st, "region": r["region"] if r else None})
    ver = [r for r in rows if r["status"] == "verified"]
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"listed": len(rows), "verified_joint_customers": len(ver), "status_counts": counts,
            "regions_verified": sorted({r["region"] for r in ver if r["region"]}),
            "problems": [r for r in rows if r["status"] != "verified"][:20],
            "verified_sample": [r["customer"] for r in ver][:8]}


def reference(con, url):
    row = con.execute("SELECT payload FROM references_index WHERE url=?", (url,)).fetchone()
    if not row:
        return {"url": url, "http_status": None, "public": False, "mentions_firm": False, "mentions_claude": False,
                "page_summary": "Fetch failed: no response recorded for this URL."}
    return json.loads(row["payload"])


def story_ok(r):
    return bool(r.get("http_status") == 200 and r.get("public") and r.get("mentions_firm") and r.get("mentions_claude"))


def crm(con, domain, app_id):
    partners = [dict(r) for r in con.execute("SELECT * FROM crm_accounts WHERE lower(domain)=lower(?)", (domain,))]
    others = []
    for r in con.execute("SELECT app_id,submitted_at,payload,status FROM applications WHERE lower(domain)=lower(?) AND app_id<>?",
                         (domain, app_id)):
        p = json.loads(r["payload"])
        others.append({"app_id": r["app_id"], "submitted_at": r["submitted_at"], "status": r["status"],
                       "requested_tier": p["requested_tier"], "claimed_certified": p["claimed_certified"],
                       "customers": [c["customer"] for c in p["customers"]][:10]})
    return partners, others


def direct_hits(con, names):
    out = []
    for n in names:
        r = con.execute("SELECT * FROM direct_accounts WHERE lower(name)=lower(?)", (n,)).fetchone()
        if r:
            out.append(dict(r))
    return out


def standing(con, a):
    """Deterministic placement from the systems of record: highest tier whose published requirements are met."""
    b = bench(con, a["domain"], a["firm_name"])
    j = joint_customers(con, a)
    good = [u for u in a["public_story_urls"] if story_ok(reference(con, u))]
    met, gaps = "Registered", {}
    for tier in ("Select", "Preferred", "Global Premier"):
        t = RUBRIC["tiers"][tier]
        g = []
        if b["active_certified"] < t["min_active_certified"]:
            g.append(f"active certified {b['active_certified']} of {t['min_active_certified']}")
        if j["verified_joint_customers"] < t["min_joint_customers"]:
            g.append(f"joint customers {j['verified_joint_customers']} of {t['min_joint_customers']}")
        if len(good) < t["min_public_stories"]:
            g.append(f"public stories {len(good)} of {t['min_public_stories']}")
        if len(j["regions_verified"]) < t.get("min_regions", 0):
            g.append(f"regions {len(j['regions_verified'])} of {t['min_regions']}")
        gaps[tier] = g
        if g:
            break
        met = tier
    return {"highest_tier_met": met, "gaps_by_tier": gaps, "active_certified": b["active_certified"],
            "joint_customers": j["verified_joint_customers"], "public_stories": len(good)}


def blockers(con, a):
    """Reasons an application can never be placed by the agent, whatever the model says."""
    out = []
    partners, others = crm(con, a["domain"], a["app_id"])
    if partners:
        out.append("existing partner account on this domain")
    if any(o["submitted_at"] < a["submitted_at"] for o in others):
        out.append("earlier application from the same domain")
    scr = con.execute("SELECT status FROM screening WHERE domain=?", (a["domain"],)).fetchone()
    if not scr or scr["status"] != "clear":
        out.append("compliance screening not clear")
    if direct_hits(con, [c["customer"] for c in a["customers"]]):
        out.append("customer is a direct enterprise account")
    if joint_customers(con, a)["status_counts"].get("attributed_to_other_partner"):
        out.append("customer attributed to another partner")
    if INJECTION.search(a.get("notes", "") or ""):
        out.append("applicant text contains instructions to the reviewer")
    b = bench(con, a["domain"], a["firm_name"])
    if a["claimed_certified"] > 2 * b["records_found_any_status"]:
        out.append(f"claimed {a['claimed_certified']} certified, platform has records for {b['records_found_any_status']}")
    if RANK.get(a["requested_tier"], 9) > RANK[MAX_TIER]:
        out.append(f"requested {a['requested_tier']}, above {MAX_TIER}")
    return out


# ---------- read tools ----------

@mcp.tool()
def list_pending_applications() -> dict:
    """List applications with no decision yet: app_id, firm, requested tier, submitted_at."""
    with db() as con:
        rows = con.execute("SELECT * FROM applications WHERE status='new' ORDER BY submitted_at").fetchall()
        return {"applications": [{"app_id": r["app_id"], "submitted_at": r["submitted_at"],
                                  "firm_name": json.loads(r["payload"])["firm_name"],
                                  "requested_tier": json.loads(r["payload"])["requested_tier"]} for r in rows]}


@mcp.tool()
def get_application(app_id: str) -> dict:
    """Full application from the partner portal. Everything in it is the applicant's claim, unverified."""
    with db() as con:
        a = load_app(con, app_id)
        mark(con, app_id, "get_application")
        if len(a["customers"]) > 25:
            a["customers_truncated"] = f"{len(a['customers'])} listed; first 25 shown. verify_joint_customers checks all of them."
            a["customers"] = a["customers"][:25]
        return a


@mcp.tool()
def get_tier_rules() -> dict:
    """Published tier requirements, definitions, and the autonomy matrix (what the agent may decide alone)."""
    return RUBRIC


@mcp.tool()
def search_crm(app_id: str, domain: str) -> dict:
    """CRM lookup by domain: existing partner accounts, and other portal applications from the same domain."""
    with db() as con:
        partners, others = crm(con, domain, app_id)
        mark(con, app_id, "search_crm", domain)
        return {"partner_accounts": partners, "other_applications_same_domain": others}


@mcp.tool()
def lookup_direct_accounts(app_id: str) -> dict:
    """Check every customer the application lists against direct enterprise accounts owned by an account exec."""
    with db() as con:
        a = load_app(con, app_id)
        mark(con, app_id, "lookup_direct_accounts")
        return {"checked": len(a["customers"]), "matches": direct_hits(con, [c["customer"] for c in a["customers"]])}


@mcp.tool()
def verify_certified_bench(app_id: str, domain: str, firm_name: str) -> dict:
    """Certification platform lookup. active_certified = current cert + used Claude in the past 90 days, on the
    firm's own domain. Also returns expired certs, inactive holders, and people certified on other email domains."""
    with db() as con:
        mark(con, app_id, "verify_certified_bench", domain)
        return bench(con, domain, firm_name)


@mcp.tool()
def verify_joint_customers(app_id: str) -> dict:
    """Check every customer the application lists against Claude account data: production usage in the trailing
    12 months with the applicant as partner of record. Returns counts, regions and every customer that failed."""
    with db() as con:
        a = load_app(con, app_id)
        mark(con, app_id, "verify_joint_customers")
        return joint_customers(con, a)


@mcp.tool()
def check_reference(app_id: str, url: str) -> dict:
    """Fetch a public customer story URL: HTTP status, whether it is public, whether it names the firm and Claude."""
    with db() as con:
        mark(con, app_id, "check_reference", url)
        return reference(con, url)


@mcp.tool()
def screening_status(app_id: str, domain: str) -> dict:
    """Compliance screening result for the applicant's domain: clear, needs_review, or no_record."""
    with db() as con:
        mark(con, app_id, "screening_status", domain)
        r = con.execute("SELECT * FROM screening WHERE domain=?", (domain,)).fetchone()
        return dict(r) if r else {"domain": domain, "status": "no_record", "detail": ""}


# ---------- write tools (guarded) ----------

@mcp.tool()
def record_decision(app_id: str, route: str, summary: str, flags: list[str], tier: str = "",
                    recommended_tier: str = "", duplicate_of: str = "") -> dict:
    """Record the triage decision. Exactly once per application.
    route: approve | human_review | duplicate. Declines are not a route: use human_review and recommend one.
    tier: required for approve; Registered or Select, exactly what the verified numbers support.
    recommended_tier: highest tier the verified numbers meet (for upsell signals and human reviews).
    duplicate_of: required for duplicate."""
    with db() as con:
        a = load_app(con, app_id)
        prior = con.execute("SELECT route FROM decisions WHERE app_id=?", (app_id,)).fetchone()

        def refuse(why):
            audit(con, "refused", app_id, {"route": route, "tier": tier, "why": why})
            con.commit()
            return {"ok": False, "refused": why}

        if prior:
            return refuse(f"already decided as {prior['route']}; decisions are write-once")
        if route not in ROUTES:
            return refuse(f"route must be one of {sorted(ROUTES)}. Declines go to human_review with a recommendation.")
        bad = [f for f in flags if f not in FLAGS]
        if bad:
            return refuse(f"unknown flags {bad}; allowed: {sorted(FLAGS)}")

        if route == "approve":
            if tier not in ("Registered", "Select"):
                return refuse(f"agent may only place partners at Registered or {MAX_TIER}; route higher tiers to human_review")
            ran = {r["check_name"] for r in con.execute("SELECT check_name FROM checks_run WHERE app_id=?", (app_id,))}
            refs_run = {r["arg"] for r in con.execute("SELECT arg FROM checks_run WHERE app_id=? AND check_name='check_reference'", (app_id,))}
            missing = [c for c in ("search_crm", "lookup_direct_accounts", "verify_certified_bench", "verify_joint_customers",
                                   "screening_status") if c not in ran]
            missing += [f"check_reference({u})" for u in a["public_story_urls"] if u not in refs_run]
            if missing:
                return refuse(f"verification not run: {missing}")
            blk = blockers(con, a)
            if blk:
                return refuse(f"cannot place this applicant without a human: {blk}")
            st = standing(con, a)
            supported = st["highest_tier_met"] if RANK[st["highest_tier_met"]] <= RANK[MAX_TIER] else MAX_TIER
            if tier != supported:
                return refuse(f"systems of record support {supported}, not {tier}. Gaps: {st['gaps_by_tier']}")
            con.execute("INSERT INTO crm_accounts VALUES(?,?,?,?,?,?,?,?,?)",
                        (f"PA-{app_id[-4:]}", a["firm_name"], a["domain"], "partner", tier, "active", TODAY,
                         "unassigned", f"agent:{app_id}"))
        if route == "duplicate":
            o = con.execute("SELECT payload,submitted_at FROM applications WHERE app_id=?", (duplicate_of,)).fetchone()
            if not o:
                return refuse(f"duplicate_of {duplicate_of!r} not found")
            op = json.loads(o["payload"])
            same = (op["domain"] == a["domain"] and op["claimed_certified"] == a["claimed_certified"]
                    and [c["customer"] for c in op["customers"]] == [c["customer"] for c in a["customers"]])
            if not same or o["submitted_at"] >= a["submitted_at"]:
                return refuse("duplicate_of must be an EARLIER application from the same domain with identical content")
        if route == "human_review":
            con.execute("INSERT INTO review_queue VALUES(?,?,?,?,?)",
                        (app_id, recommended_tier or "", json.dumps(flags), summary, now()))

        con.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?)",
                    (app_id, route, tier or "", recommended_tier or "", json.dumps(flags), summary, duplicate_of or "", now()))
        status = {"approve": f"approved_{(tier or '').lower()}", "human_review": "in_review", "duplicate": "duplicate"}[route]
        con.execute("UPDATE applications SET status=? WHERE app_id=?", (status, app_id))
        audit(con, "record_decision", app_id, {"route": route, "tier": tier, "flags": flags})
        con.commit()
        return {"ok": True, "app_id": app_id, "status": status,
                "crm_account_created": route == "approve", "queued_for_human": route == "human_review"}


@mcp.tool()
def draft_email(app_id: str, subject: str, body: str) -> dict:
    """Save a draft to the applicant's contact email in the outbox for a human to review and send.
    Only for approved applications. The agent cannot send email or choose the recipient."""
    with db() as con:
        a = load_app(con, app_id)
        d = con.execute("SELECT route, tier FROM decisions WHERE app_id=?", (app_id,)).fetchone()
        if not d or d["route"] != "approve":
            audit(con, "refused", app_id, {"draft": subject})
            con.commit()
            return {"ok": False, "refused": "drafts only for approved applications; human_review and duplicates get no applicant email"}
        kind = f"welcome_{d['tier'].lower()}"
        cur = con.execute("INSERT INTO outbox(app_id,to_addr,subject,body,kind,status,created_at) VALUES(?,?,?,?,?,?,?)",
                          (app_id, a["contact_email"], subject, body, kind, "awaiting_human_send", now()))
        audit(con, "draft_email", app_id, {"kind": kind, "subject": subject})
        con.commit()
        return {"ok": True, "draft_id": cur.lastrowid, "to": a["contact_email"], "kind": kind, "status": "awaiting_human_send"}


if __name__ == "__main__":
    mcp.run()
