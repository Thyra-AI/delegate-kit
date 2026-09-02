---
name: python
title: Python — defect taxonomy (asyncio, FastAPI, SQLAlchemy) for the bug-hunter
url: https://docs.python.org/3/
agents: bug-hunter
tools:
---

## Python — what these classes look like here

The classes above are the general shape. In Python, hunt for these specifically. Everything here still goes through the same self-check: verbatim quote, concrete scenario, confidence floor.

**async.** — a coroutine never awaited (the result discarded, or used as a truthy value, or attribute-accessed); a blocking call inside `async def` (`time.sleep`, `requests`, sync DB or file IO); `asyncio.create_task` with no reference kept — it can be garbage-collected mid-flight — and no exception handling; `gather` without `return_exceptions` where a partial failure leaves inconsistent state; a lock held across an `await` that can re-enter; a sync `Session` used across an `await`.

**data.** — a commit with no rollback on the exception path; a session leaked or shared across requests or tasks; a missing `await session.commit()`; a lazy-load on an `AsyncSession` (`MissingGreenlet` at runtime); check-then-insert races with no unique constraint behind them; bulk `update()`/`delete()` bypassing ORM events the surrounding code relies on; a multi-step write with no transaction. A query missing tenant/user/org scoping is `authz.*` and **high**, not `data.*`.

**authz.** — an endpoint missing its auth dependency; identity read from the body or query string instead of the token; an inverted or incomplete role check; route ordering that shadows (`/{id}` declared before `/me`); a missing `response_model` letting internal fields leak; a dependency carrying mutable state across requests.

**error.** — a bare `except:` or a broad `except Exception` that logs and continues as if the call succeeded; an error returned as HTTP 200; a `finally` with `return`/`break` swallowing the exception in flight; `HTTPException` raised inside a background task or inside a generator after the first yield, where it becomes a 500 or vanishes entirely.

**resource.** — an `httpx`/`aiohttp` client, file, or stream opened without `with`/`async with` and never closed; a client constructed per request instead of reused; an async generator not closed on client disconnect; a `NamedTemporaryFile` that outlives its use.

**sec.** — SQL built with an f-string or passed through `text()` from user input; `subprocess` with `shell=True` on user input; `eval`/`exec`; `yaml.load` without `SafeLoader`; `pickle` on untrusted data; a request to a user-supplied URL (SSRF); `os.path.join` on user input (traversal); `allow_origins=["*"]` together with `allow_credentials=True`.

**state.** — a module-level dict, list, or cache mutated by request handlers; an unbounded `lru_cache` or hand-rolled cache on a per-user key; cross-worker state assumed consistent under gunicorn/uvicorn workers.

**stream.** — add this prefix for streaming-specific findings: a streaming generator that leaks its upstream connection on disconnect; chunk parsing that drops or duplicates content across boundaries; a retry that is not idempotent; provider errors (auth, quota, rate limit) flattened into a generic 500.

## Python-only traps

These have no general-language equivalent above, so look for them explicitly: a mutable default argument; `is` used on `str` or `int`; a collection mutated while iterating over it; late binding in a closure created in a loop (`lambda: i`); a naive and an aware `datetime` compared or subtracted; `d["key"]` on external JSON where `.get` was meant; an enum member compared to a raw string; `/` where `//` was meant; a shadowed builtin used later in the same scope; `__del__` or `weakref` relied on for cleanup ordering.

## Do not report in Python

Missing type hints, `Any` in annotations, f-string vs `%` formatting, import ordering, `Optional[X]` vs `X | None`, a `dataclass` that could be a `NamedTuple`, or a `try` block that is wider than it needs to be but is still correct. And unless the brief says otherwise, treat test files and generated modules as out of scope — do not follow imports into them.
