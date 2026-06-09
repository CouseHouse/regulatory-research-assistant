# A third of citation quotes failed verification — and the corpus wasn't the cause

## Symptom

After activating quote-faithfulness checking — matching each analyst quote against the
cited source chunk at similarity threshold τ = 0.85 — only 309 of 446 quotes verified.
About 30% were failing. The deterministic citation gate had been passing at 1.000 the
day before, but that ruler only checked that a citation pointed at a real chunk, not
that the quoted text actually appeared in it.

## First hypothesis

My first read was that the corpus was dirty. The FDA guidances are PDFs, the chunker had
known boilerplate and boundary issues, and "the quotes don't match the source text"
sounds exactly like a chunking and cleaning problem. The plan was to re-chunk and clean
the corpus, then re-measure.

## What I tried first — the diagnostic

Before paying to re-embed a cleaned corpus, I ran a $0 text-only smoke that matched the
same quotes against two corpus arms: the existing dirty fixed-size chunks (`chunks`) and
a cleaned, structurally re-chunked copy (`chunks_rechunk`). If the corpus were the
problem, the clean arm should verify more quotes.

It didn't. Both arms verified exactly 309/446 — delta zero. The cleaner corpus bought
nothing, which ruled it out and pointed at the one thing both arms shared: the matcher.

## What turned out to be the actual problem

Two preprocessing gaps in the quote/chunk normalizer (`mcp_server/tools.py` `_normalize`):

1. **Smart quotes.** The PDFs use curly quotes and apostrophes (U+2018/2019/201C/201D…);
   the analyst emitted ASCII, so byte-equal substring matches missed.
2. **PDF line numbers.** pypdf splices sequential line numbers into FDA draft text
   (`"medical 105 \ndevices"`), corrupting the quoted span.

## The fix

Normalize curly punctuation to ASCII and strip inline PDF line numbers — with lookbehind
guards so real references like "21 CFR 820" survive — applied to both sides before
comparison.

## Before / after

| Metric | Before | After |
|---|---|---|
| Quotes verified (τ = 0.85) | 309/446 | 386/446 |
| recall@10 | 1.00 (13/13) | 1.00 (13/13) |

Smart-quote normalization rescued +41; line-number stripping a further +36. recall@10 was
already 1.00 and stayed there — this was never a retrieval problem.

This didn't close the gap completely — 60 quotes still fail. Triaging them showed the split
that matters: ~39 are matcher/corpus near-misses (boundary straddle, wrong-chunk citations),
but ~21 are genuine analyst faithfulness issues (synthesized text and ellipsis stitches) that
no matcher fix can rescue. The preprocessing recovered the noise; the real residual is the
analyst, and it's deferred rather than solved.

## What I'd do differently

The diagnostic — comparing corpus arms before spending on a re-embed — is the part I'd
keep. The mistake was reaching for the expensive, plausible fix (re-chunk the corpus)
before running the cheap test that would tell me where the problem actually was. About $0
and twenty minutes of smoke testing saved a re-embed and a wrong conclusion.
