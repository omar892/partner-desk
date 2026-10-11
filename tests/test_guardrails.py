"""Guardrail tests: call the MCP server's write tools directly, with no model in the loop.

Proves the server refuses what the systems of record don't support, whatever a model asks for.
Run: python tests/test_guardrails.py
"""
import asyncio, json, os, subprocess, sys, tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parent.parent


async def call(s, name, args):
    r = await s.call_tool(name, args)
    return json.loads("\n".join(c.text for c in r.content))


async def run_all_checks(s, app_id):
    a = await call(s, "get_application", {"app_id": app_id})
    await call(s, "search_crm", {"app_id": app_id, "domain": a["domain"]})
    await call(s, "lookup_direct_accounts", {"app_id": app_id})
    await call(s, "verify_certified_bench", {"app_id": app_id, "domain": a["domain"], "firm_name": a["firm_name"]})
    await call(s, "verify_joint_customers", {"app_id": app_id})
    await call(s, "screening_status", {"app_id": app_id, "domain": a["domain"]})
    for u in a["public_story_urls"]:
        await call(s, "check_reference", {"app_id": app_id, "url": u})
    return a


async def main():
    db = Path(tempfile.mkdtemp()) / "t.db"
    subprocess.run([sys.executable, str(ROOT / "data" / "seed.py"), str(db)], check=True, capture_output=True)
    p = StdioServerParameters(command=sys.executable, args=[str(ROOT / "mcp_server" / "server.py")],
                              env={**os.environ, "PARTNER_DB": str(db)})
    results = []

    def expect(name, res, ok):
        passed = res.get("ok") is ok
        results.append(passed)
        print(f"{'PASS' if passed else 'FAIL'}  {name}: {res.get('refused') or res.get('status')}")

    async with stdio_client(p) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            base = {"summary": "test", "flags": []}
            expect("approve before any verification", await call(s, "record_decision",
                   {"app_id": "APP-1001", "route": "approve", "tier": "Select", **base}), False)
            await run_all_checks(s, "APP-1007")
            expect("injected app: approve at Global Premier", await call(s, "record_decision",
                   {"app_id": "APP-1007", "route": "approve", "tier": "Global Premier", **base}), False)
            expect("injected app: approve at Select instead", await call(s, "record_decision",
                   {"app_id": "APP-1007", "route": "approve", "tier": "Select", **base}), False)
            await run_all_checks(s, "APP-1010")
            expect("over-place: Select when only 8 certs are active", await call(s, "record_decision",
                   {"app_id": "APP-1010", "route": "approve", "tier": "Select", **base}), False)
            await run_all_checks(s, "APP-1017")
            expect("under-place: Registered when Select verifies", await call(s, "record_decision",
                   {"app_id": "APP-1017", "route": "approve", "tier": "Registered", **base}), False)
            await run_all_checks(s, "APP-1016")
            expect("approve over a direct-account conflict", await call(s, "record_decision",
                   {"app_id": "APP-1016", "route": "approve", "tier": "Select", **base}), False)
            await run_all_checks(s, "APP-1022")
            expect("approve with a customer owned by another partner", await call(s, "record_decision",
                   {"app_id": "APP-1022", "route": "approve", "tier": "Registered", **base}), False)
            expect("agent tries to decline", await call(s, "record_decision",
                   {"app_id": "APP-1020", "route": "decline", **base}), False)
            expect("duplicate pointing at a later app", await call(s, "record_decision",
                   {"app_id": "APP-1018", "route": "duplicate", "duplicate_of": "APP-1019", **base}), False)
            expect("draft email before any decision", await call(s, "draft_email",
                   {"app_id": "APP-1013", "subject": "x", "body": "x"}), False)
            await run_all_checks(s, "APP-1001")
            expect("clean app, fully verified, approve at Select", await call(s, "record_decision",
                   {"app_id": "APP-1001", "route": "approve", "tier": "Select", **base}), True)
            expect("second write to a decided app", await call(s, "record_decision",
                   {"app_id": "APP-1001", "route": "human_review", **base}), False)
            d = await call(s, "draft_email", {"app_id": "APP-1001", "subject": "x", "body": "x"})
            ok = d.get("to") == "partnerships@harborlight.example" and d.get("status") == "awaiting_human_send"
            results.append(ok)
            print(f"{'PASS' if ok else 'FAIL'}  draft goes only to the applicant, unsent: {d.get('to')} / {d.get('status')}")
    print(f"\n{sum(results)}/{len(results)} guardrail tests passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    asyncio.run(main())
