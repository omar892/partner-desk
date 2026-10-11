"""Partner Desk agent: triages every pending partner application through the partner-systems MCP server.

Claude (Messages API, tool use) <-> MCP client <-> mcp_server/server.py (stdio) <-> systems of record.

Usage:
  python agent/run.py --model sonnet --label sonnet-r1        # fresh DB, all 22 dev applications
  python agent/run.py --model haiku --set holdout --label holdout/haiku-r1   # the 10 holdout applications
  python agent/run.py --model haiku --only APP-1007           # one application

Auth: ANTHROPIC_API_KEY (direct). Falls back to OPENROUTER_API_KEY (same Messages API, Claude models).
"""
import argparse, asyncio, json, os, shutil, sqlite3, subprocess, sys, time
from pathlib import Path

import anthropic
from mcp import ClientSession, StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parent.parent
TODAY = "2026-10-10"
MAX_TURNS = 20
ALIASES = {  # alias -> (direct Anthropic id, OpenRouter id, $/M input, $/M output)
    "sonnet": ("claude-sonnet-5-5", "anthropic/claude-sonnet-5.5", 2.0, 10.0),
    "haiku": ("claude-haiku-5-5", "anthropic/claude-haiku-5.5", 0.10, 0.50),
    "opus": ("claude-opus-5-5", "anthropic/claude-opus-5.5", 4.0, 20.0),
}


def make_client():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return anthropic.AsyncAnthropic(max_retries=4, timeout=120), "anthropic"
    return anthropic.AsyncAnthropic(base_url="https://openrouter.ai/api", auth_token=os.environ["OPENROUTER_API_KEY"],
                                    api_key=None, max_retries=4, timeout=120), "openrouter"


def skill_text():
    t = (ROOT / "skills" / "partner-application-triage" / "SKILL.md").read_text()
    return t.split("---", 2)[2].strip() if t.startswith("---") else t


def usage_cost(u, price_in, price_out):
    extra = getattr(u, "model_extra", None) or {}
    if "cost" in extra and extra["cost"] is not None:
        return float(extra["cost"])
    cr = getattr(u, "cache_read_input_tokens", 0) or 0
    cw = getattr(u, "cache_creation_input_tokens", 0) or 0
    return (u.input_tokens * price_in + cr * price_in * 0.1 + cw * price_in * 1.25 + u.output_tokens * price_out) / 1e6


async def triage(app_id, session, client, model, tools, system, prices, sem):
    async with sem:
        t0 = time.time()
        msgs = [{"role": "user", "content": f"Triage application {app_id}. Today is {TODAY}."}]
        steps, usage, cost, err = [], {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}, 0.0, None
        for turn in range(MAX_TURNS):
            try:
                r = await client.messages.create(model=model, max_tokens=2500, system=system, tools=tools, messages=msgs)
            except Exception as e:  # API exhausted its retries: stop cleanly, harness escalates below
                err = f"model call failed: {type(e).__name__}: {str(e)[:200]}"
                break
            u = r.usage
            usage["input"] += u.input_tokens; usage["output"] += u.output_tokens
            usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
            usage["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
            cost += usage_cost(u, *prices)
            msgs.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in r.content]})
            for b in r.content:
                if b.type == "text" and b.text.strip():
                    steps.append({"turn": turn, "type": "text", "text": b.text.strip()[:1500]})
            if r.stop_reason != "tool_use":
                break
            results = []
            for b in r.content:
                if b.type != "tool_use":
                    continue
                ts = time.time()
                try:
                    res = await asyncio.wait_for(session.call_tool(b.name, b.input), timeout=60)
                    text = "\n".join(c.text for c in res.content if getattr(c, "type", "") == "text")
                    is_err = bool(getattr(res, "is_error", False))
                except Exception as e:
                    text, is_err = f"tool call failed: {type(e).__name__}: {e}", True
                steps.append({"turn": turn, "type": "tool", "tool": b.name, "input": b.input, "output": text[:2500],
                              "is_error": is_err, "ms": int((time.time() - ts) * 1000)})
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": text, "is_error": is_err})
            msgs.append({"role": "user", "content": results})
        else:
            err = f"hit {MAX_TURNS}-turn limit"
        return {"app_id": app_id, "steps": steps, "turns": turn + 1, "usage": usage, "cost_usd": round(cost, 6),
                "latency_s": round(time.time() - t0, 2), "error": err}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--label", default=None)
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--concurrency", type=int, default=5)
    ap.add_argument("--set", choices=["dev", "holdout"], default="dev")
    args = ap.parse_args()

    client, backend = make_client()
    direct_id, or_id, pin, pout = ALIASES.get(args.model, (args.model, args.model, 3.0, 15.0))
    model = direct_id if backend == "anthropic" else or_id
    label = args.label or f"{args.model}-{time.strftime('%Y%m%d-%H%M%S')}"
    run_dir = ROOT / "runs" / label
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    db = run_dir / "partner_ops.db"
    seeder = "seed_holdout.py" if args.set == "holdout" else "seed.py"
    subprocess.run([sys.executable, str(ROOT / "data" / seeder), str(db)], check=True, capture_output=True)

    system = [{"type": "text", "text": f"You are Partner Desk, the partner team's triage agent.\n\n{skill_text()}",
               "cache_control": {"type": "ephemeral"}}]
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "mcp_server" / "server.py")],
                                   env={**os.environ, "PARTNER_DB": str(db), "PARTNER_TODAY": TODAY})
    t0 = time.time()
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = (await session.list_tools()).tools
            tools = [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema} for t in listed]
            tools[-1]["cache_control"] = {"type": "ephemeral"}
            pending = json.loads((await session.call_tool("list_pending_applications", {})).content[0].text)
            ids = sorted(p["app_id"] for p in pending["applications"])
            if args.only:
                ids = [i for i in ids if i in args.only]
            sem = asyncio.Semaphore(args.concurrency)
            results = await asyncio.gather(*[triage(i, session, client, model, tools, system, (pin, pout), sem) for i in ids])

            # Graceful failure: anything the agent didn't decide goes to a human, never silently dropped.
            con = sqlite3.connect(db)
            decided = {r[0] for r in con.execute("SELECT app_id FROM decisions")}
            con.close()
            for res in results:
                if res["app_id"] not in decided:
                    why = res["error"] or "agent ended without recording a decision"
                    out = await session.call_tool("record_decision", {
                        "app_id": res["app_id"], "route": "human_review", "flags": ["agent_incomplete"],
                        "summary": f"Agent did not finish ({why}). Needs a manual triage."})
                    res["fallback"] = out.content[0].text

    with open(run_dir / "trace.jsonl", "w") as f:
        for res in results:
            f.write(json.dumps(res) + "\n")
    summary = {"label": label, "set": args.set, "model": model, "backend": backend, "apps": len(results),
               "wall_s": round(time.time() - t0, 1), "cost_usd": round(sum(r["cost_usd"] for r in results), 4),
               "tool_calls": sum(1 for r in results for s in r["steps"] if s["type"] == "tool"),
               "tool_errors": sum(1 for r in results for s in r["steps"] if s["type"] == "tool" and s["is_error"]),
               "fallbacks": sum(1 for r in results if "fallback" in r),
               "median_latency_s": sorted(r["latency_s"] for r in results)[len(results) // 2] if results else 0}
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary))


if __name__ == "__main__":
    asyncio.run(main())
