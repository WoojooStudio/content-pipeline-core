# content-pipeline-core

The reliability core of the content automation I run for my own product's social channels. It covers the content
state machine, human approval gates, approval hashes, an append-only audit ledger, and an idempotent publisher.

This is an extracted public snapshot. The full system around it is private: topic generation, the writer and
reviewer agents, the web console, analytics, and the brand-specific copy lint. What's here is the part that decides
**what is allowed to go out, and proves what went out.**

- Python 3.11+, standard library only (`jsonschema` and `pytest` for tests)
- 118 tests
- Write-up of the incident this code was shaped by: [The first live publish](https://hanna-kang-systems.vercel.app/cases/first-live-publish/)

```bash
uv run --extra dev pytest -q
```

## The pipeline it guards

```
brief → topic ideas → [Gate 0 · human picks] → draft → lint + review by a second model
      → [Gate 1 · human approves text] → production (images / reels, hashed assets)
      → [Gate 2 · human approves the exact final version → approval hash]
      → publish (API) or hand-off to a human who posts → recorded + metrics
```

Every gate is enforced in code, not by convention. **An agent cannot skip a gate, and nothing irreversible happens
without a human approval.**

## What's in here

### `core/`

| Module | What it does |
|---|---|
| `status.py` | Derives a content item's status from its lifecycle timestamps. Status is never stored on its own, so it can't drift. |
| `gate.py` | `submit_gate` / `record_gate` / `require_gate1` / `require_gate2`. A gate needs every check passed and a named approver. |
| `approval.py` | The **approval hash**: one canonical digest over id, channel, format, draft, caption and every asset in order, with each asset's sha256 **re-read from disk**. `approval_problem()` says whether an approval is still valid: no hash, production reopened after approval, or a mismatch all invalidate it. Change one character after approval and the approval is void. |
| `production.py` | Records production start and completion. It validates the asset manifest: relative paths inside the content folder only, no `..`, no symlink escape, no hard links, no duplicate inode, extension by role, and format completeness (a carousel needs 2–10 slides, a reel exactly one video). |
| `ledger.py` | Append-only JSONL audit trail of every state change. |
| `idempotency.py` | Job-id and output-exists checks, so re-running a step is safe. |
| `index.py` · `counter.py` · `slots.py` · `plan.py` · `schedule.py` | The derived index, id numbering (archived numbers are never reused), publishing slots and cadence plans. |
| `schemas/` | JSON Schemas for content (v3–v6, with readers that accept older versions), plans, profiles, batches and picks. `additionalProperties: false` throughout, so unknown fields are rejected. |

### `publisher/`

| Module | What it does |
|---|---|
| `service.py` | The **only** publish entry point. Enforced in this order: id check and load → **policy** (auto-publish must be on for the channel) → **gates** (Gate 2 valid, approval hash still matching) → **signature** → **nonce** reserve → send → write back. |
| `nonce.py` | A one-time token per publish, tied to the payload hash: `reserved → publishing → consumed`. It's meant to stop the same post from going out twice. |
| `policy.py` | Per-channel publish policy, read from the channel profile. |
| `signature.py` · `artifact.py` | Signed publish artifacts, so what is sent is exactly what was approved. |
| `oauth.py` · `credentials.py` · `transport.py` | Token handling with pure request/response functions and an injectable transport, so everything is testable without sockets. Credentials are injected last and never logged. |
| `adapters/` | Threads, Instagram and Blogger (draft upload only — there is no code path that makes a post public). |

## Known gap — documented, not hidden

`nonce.reconcile()` can only repair a run that already recorded `published`. If a run stops **between sending and
writing back**, it has no way out. A re-run would reuse the same nonce, pass the guard, and send again. That is a
possible double post if the first send landed.

This was found after the first live publish stalled halfway, and it's described in the
[incident write-up](https://hanna-kang-systems.vercel.app/cases/first-live-publish/). Live publishing on that path is
fenced off until a recovery contract exists. The fix must not become a way around the approval gates. The candidates
are a guard that refuses to send from `publishing` without a human decision, a `reconcile` path for "sent, not
confirmed", and logging every outbound request before trusting a state change.

## How it was built

I designed the pipeline, the gates and the contracts. AI coding agents (Claude Code, Codex) implemented the modules
against those contracts, with a separate model reviewing. I reviewed, tested and debugged. Rules outrank tests, and
tests outrank implementation.

The code comments were translated from Korean for this snapshot. A small helper module (`core/content_paths.py`) was
moved out of a private dashboard module so the publisher runs on its own.

## License

MIT — Hanna Kang · [systems portfolio](https://hanna-kang-systems.vercel.app)
