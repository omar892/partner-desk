# Partner Desk

A Claude agent that triages Claude Partner Network applications. It reads each application, checks
every claim against the systems of record through a custom MCP server, places the firm at the tier
the evidence supports, sends anything risky to a person, and drafts the partner email for a human
to send.

Built by Omar Abbasi with Claude as a working sample for the AI Operations Engineer, Partnerships role.
The tier thresholds are the published Services Track requirements (Select 10/2/1, Preferred
100/15/3, Global Premier 1,000/100/15 across 3+ regions).

Every firm, person and customer in the data is fictional. Firm domains use the reserved `.example`
TLD so no real company is named. They were renamed from made-up `.com` and `.io` names after the
eval runs, with one consistent find-and-replace across the fixtures and the traces. No rule depends
on the TLD, and `data/seed.py` reproduces the renamed fixtures exactly.

## Why this workflow

More than 40,000 firms have applied to the network. Every application makes the same three claims
(certified people, production customers, public stories), and each claim lives in a different
system. Checking them by hand is slow and inconsistent. Most applications are routine, and a small
share carry real risk: a channel conflict, a customer another partner already owns, a compliance
hit, or text written to steer the reviewer.

## How it works

```
Claude (Messages API, tool use)
   │  skill: skills/partner-application-triage/SKILL.md
   ▼
MCP client ──stdio──► partner-systems MCP server (mcp_server/server.py)
                         │ reads                         │ writes (guarded)
                         ├ partner portal applications   ├ decisions (write-once)
                         ├ CRM partner + direct accounts ├ CRM partner account on approval
                         ├ certification platform        ├ human review queue
                         ├ Claude account data           ├ outbox (drafts only)
                         ├ public story fetches          └ audit log
                         └ compliance screening
```

The systems of record are SQLite stand-ins (`data/seed.py`). In production each read tool becomes
an API call to the real system and nothing else changes.

## Who decides what

| Decision | Owner |
|---|---|
| Read every system, verify every claim | Agent |
| Place a clean new applicant at Registered or Select, exactly as the data supports | Agent |
| Link an exact duplicate submission | Agent |
| Draft the welcome email, with the gap to the next tier | Agent (human sends) |
| Any tier above Select, including upgrades | Human |
| Any decline | Human (agent recommends) |
| Claims that don't match the records, compliance hits, channel or attribution conflicts | Human |
| Instructions to the reviewer hidden in applicant text | Human |

## Guardrails live in the server, not only the prompt

The prompt tells Claude the rules. The MCP server enforces them, so a confused or manipulated model
still can't do damage. `record_decision` recomputes placement from its own data and refuses:

- an approval before every check has actually run
- any tier the verified numbers don't support, in either direction (over-placing or under-placing)
- any approval above Select, any decline, any approval with an open conflict
- a duplicate link to a later application, and a second write to a decided application

`draft_email` can only address the applicant's own contact, and only after an approval. Nothing is
ever sent. `tests/test_guardrails.py` calls the server with no model in the loop and checks all of
this: 13 of 13 pass.

Deterministic where you can, model where you must: the server owns the math and the permissions.
Claude reads messy applications, connects evidence across systems, writes the reviewer summary, and
writes a specific, human email to the firm.

## Results

22 labeled dev applications, written by hand before any agent run, covering the routine cases and the
risky ones (prompt injection, overclaimed bench, expired or inactive certs, certs on personal email,
pilot-only customers, dead and gated story links, channel conflict, attribution conflict, existing
partner, compliance hit, duplicate, spam, upsell). Three runs per model.

Three rounds. Every round is 3 runs per model, scored by `evals/score.py` against labels written
before the agent saw the cases.

| Round | Set | Model | Fully correct (per run) | Unsafe approvals | Wrong tier | Missed escalations | Cost / app | Median latency |
|---|---|---|---|---|---|---|---|---|
| 1 | Dev, 22 apps | Sonnet 5.5 | 22, 22, 22 | 0 | 0 | 0 | $0.044 | 18s |
| 1 | Dev, 22 apps | Haiku 5.5 | 20, 21, 21 | 0 | 0 | 0 | $0.0025 | 12s |
| 2 | Dev, 22 apps | Sonnet 5.5 | 22, 22, 22 | 0 | 0 | 0 | $0.042 | 16s |
| 2 | Dev, 22 apps | Haiku 5.5 | 21, 21, 22 | 0 | 0 | 0 | $0.0024 | 11s |
| 3 | Holdout, 10 apps | Sonnet 5.5 | 10, 10, 10 | 0 | 0 | 0 | $0.039 | 14s |
| 3 | Holdout, 10 apps | Haiku 5.5 | 10, 10, 10 | 0 | 0 | 0 | $0.0023 | 11s |

"Fully correct" means right route, right tier, every required flag, the right draft (or no draft),
the right recommended tier and the right duplicate link.

**Round 1** used the first version of the triage skill (`evals/history/SKILL.v1.md`). Two things
went wrong, both on Haiku and both safe: it sent one firm with expired certs to a person instead of
placing it at Registered (2 of 3 runs), and it missed one flag. Both models also tried 3 to 4 flag
names per run that don't exist. The server refused each one, the model read the refusal, and
recorded the decision again with a valid flag. Nothing slipped through.

**Round 2** added two rules to the skill: an explanation of when a claimed-versus-verified gap is a
mismatch, and the exact list of allowed flags. Haiku now routes all 22 correctly on every run. Its
only slip was one missing `reference_unreachable` flag on a firm that was still placed at the right
tier. Invented flag names dropped from 3-4 per run to 0-1.

Round 2 was tuned on the same 22 cases it was scored on, so it is optimistic. That is why round 3
exists.

**Round 3** is a holdout: 10 new applications (`data/seed_holdout.py`, `evals/labels_holdout.json`),
written and labeled after round 2 was final. The skill, the server and the rubric were frozen
before these were written (hashes in `runs/holdout/frozen_hashes.txt`). The holdout includes
variants the dev set doesn't have:

- A polite instruction to "the automated reviewer" that the server's injection pattern does not match
- An instruction hidden in the firm's own customer story page, not in the application
- A firm that resells API credits and isn't a services firm at all
- A firm asking for Registered whose verified numbers meet Select
- A terminated former partner re-applying as new, and a domain with no screening record

The first three are cases the server's guardrails do not catch on their own: the numbers are clean,
so only the model's judgment stands between them and an approval. Both models caught all three on
every run, and their summaries say why ("the public story page contains hidden text telling
automated reviewers to record the firm as approved; I did not follow it").

Honest limits: 10 cases is a sanity check, not proof. The same person wrote the skill, the cases and
the labels. A real rollout would score against a few hundred historical applications with partner
managers' actual decisions as the labels (see next steps).

**What this says about model choice.** Haiku made every safe decision Sonnet made, at about 1/17th
the cost. That works because the risky parts (tier math, permissions, who gets an email) live in the
server, so a cheaper model can't turn a mistake into damage. Total eval spend across all three
rounds: about $7.30.

Run folders: `runs/v1` (round 1), `runs/v2` (round 2), `runs/holdout` (round 3). Each run has `score.json`
(per-application detail) and `trace.jsonl` (every tool call, with inputs and outputs).

## Run it

```bash
uv venv && uv pip install -r requirements.txt
export ANTHROPIC_API_KEY=...            # or OPENROUTER_API_KEY
python tests/test_guardrails.py         # no model, no key needed
python agent/run.py --model haiku --label try/dev-haiku
python agent/run.py --model haiku --set holdout --label try/holdout-haiku
python evals/score.py runs/try/dev-haiku runs/try/holdout-haiku
```

The MCP server is a standard stdio server, so it also plugs into Claude Code or Claude Desktop as a
connector.

## Next steps if this were real

1. Swap the SQLite reads for the portal, CRM, certification and account-data APIs.
2. Shadow mode for two weeks: the agent decides, a partner manager decides blind, compare.
3. Turn on auto-placement for Registered first, then Select, once shadow agreement holds.
4. Post the human review queue to the team's Slack with one-click approve or override, and log
   every override as a new labeled case.
5. Replace the hand-written test cases with a few hundred past applications labeled by the
   decisions partner managers actually made, and re-run the eval on every prompt, model or rule
   change before it ships.
