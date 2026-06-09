# An LLM-as-judge that silently scored nothing

## Symptom

The first full eval baseline came back with `key_fact_coverage` returning N/A on all 30
cases — `score=None` everywhere. Citation validity (1.000) and position quality (0.973)
reported fine; the one judge meant to catch under-citing produced no signal at all.

## First hypothesis

`score=None` on every case looks exactly like a scorer that was never wired up — a wrong
DB lookup, a missing graph output, an exception swallowed into a default. So the first
thing I checked was the plumbing: was the scorer actually receiving the answer text and
the `expected_facts`, and reaching the judge model at all?

## What turned out to be the actual problem

The wiring was fine. The judge — Haiku — was being called and was answering, but it
wrapped its JSON verdict in conversational prose ("Here is the JSON: …"). The scorer
parsed with a strict `json.loads`, which rejected the wrapped output; both the call and
its one retry failed the same way, and the scorer fell back to `score=None`. Every case
hit the identical failure, which is why it looked like a dead scorer rather than a flaky
one.

## The fix

Prefill the assistant turn with an opening `{` (Anthropic's prefill parameter) in
`judge.py`, forcing the model to emit raw JSON from the first token with no room for a
prose preamble. Strict parsing, the single retry, and the None-on-double-failure fallback
all stayed — the fix removed the failure mode rather than loosening the parser.

## Before / after

| Metric | Before | After |
|---|---|---|
| `key_fact_coverage` signal | 0/30 (all N/A) | 30/30 scored |
| `key_fact_coverage` mean | — | 0.908 |
| Pass rate (≥ 0.80) | — | 66.7% (20/30) |

The scorer didn't just start emitting numbers — it discriminated. A third of cases land
below threshold, which is the under-citing failure mode it exists to catch.

## What I'd do differently

Two things. First, a judge that can't be parsed should fail loud, not silently degrade to
None — `None` and "genuinely no citations to score" looked identical and masked the bug
for a whole baseline run. I'd separate "judge unparseable" from "nothing to score."
Second, constrain output format at the source (prefill or a tool schema) from the first
judge call rather than trusting a free-text model to stay inside JSON. The related design
lesson the harness already bakes in — giving the position-quality judge the source
passages so it can't reward a confident hallucination (`spec.md` §6) — is the same
principle from the other direction: don't trust an LLM judge's unconstrained output,
structurally or substantively.
