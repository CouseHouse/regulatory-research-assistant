# 503 from the load balancer: the serving image was never built, then the fix looked like it failed

## Symptom

After the private-RDS bootstrap work deployed, the ALB returned **503 on both `/health`
and `/readyz`** — no healthy target behind it. Read-only AWS triage (target health →
service events → stopped tasks → CloudWatch) showed the ECS service crash-looping:
`runningCount=0`, tasks living ~60 s (start → register → drain → restart), each exiting `1`.

## First hypothesis

A 503 with zero healthy targets usually means a health-check misconfiguration — wrong
path, wrong port, or a grace period too short for a slow boot. That's where I started.
The CloudWatch logs killed that idea immediately: the `app` container wasn't running
uvicorn at all. It was running `python -m rra.ingest`, which hit the known FDA-IP download
block (`failed=50 succeeded=0`), logged `corpus.ingest.no_files_downloaded`, and exited 1.
uvicorn never started → nothing on `:8000` → no healthy target → 503. The health check was
fine; the wrong process was running.

## Root cause #1 — the wrong Dockerfile stage shipped

The serving task def ran the image's own entrypoint, which was `python -m rra.ingest`. The
`:latest` image had been built from the Dockerfile's **last stage (`bootstrap`)**, not
`runtime` — a bare `docker build .` defaults to the last stage, and the serving image needs
`--target runtime`. ECR confirmed no serving image had ever been pushed.

## The fix that looked like it failed

I rebuilt `--target runtime`, smoke-tested locally (`docker inspect` → `CMD=[uvicorn …]`,
ran it, saw "Uvicorn running"), and pushed the correct image to `:latest`. **Still 503.**
The obvious read was that the new image was also broken. It wasn't. `describe-tasks` showed
the tasks' *resolved* `imageDigest` was still the old ingest image — even for tasks launched
*after* the push. **An ECS service deployment resolves its image tag to a digest once, at
deployment creation, and pins it.** New `:latest` pushes are invisible to a live deployment.
Every "the push didn't help" was this, not a bad image.

## The fix

`aws ecs update-service … --force-new-deployment` re-resolved `:latest` to the uvicorn
digest. The target went **healthy**, `/readyz` returned `{"status":"ready"}`, and `/query`
returned a full multi-agent RAG answer with citations.

## Before / after

| State | Before | After |
|---|---|---|
| ALB `/readyz` | 503 (0 healthy targets) | 200 `{"status":"ready"}` |
| ECS `runningCount` | 0 (crash-loop, exit 1) | steady |
| Resolved image | ingest stage (`rra.ingest`) | runtime stage (uvicorn) |

## What I'd do differently

Both traps vanish with **immutable image tags** (`serve-<sha>` instead of `:latest`): a push
becomes a task-def change, so digest pinning can't mislead and the running image is
unambiguous. Until then, gate pushes on `docker inspect … CMD` so a wrong-stage build can't
ship silently, and reorder the Dockerfile so `runtime` is the default last stage. The deeper
lesson is the masked fix: when a correct change appears to do nothing, suspect that something
between you and production is serving a stale version — don't assume the change was wrong.
