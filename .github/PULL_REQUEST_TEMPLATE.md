## What this changes

<!-- And why. If it invalidates a decision recorded in docs/decisions.md, update that
     document in this pull request. -->

## Checks

- [ ] `uv run --extra dev ruff check .`
- [ ] `uv sync --extra dev && uv run --no-sync pytest -q` — passes **without** the extras
      (expect 1 skipped: `tests/test_mcp_server.py`)
- [ ] `uv run --extra dev --extra mcp pytest -q` — passes **with** them
- [ ] `cd frontend && npm run build && npm test` — if `frontend/` was touched

## Live checks

The offline suite cannot see an external service behaving differently from how we imagined
it. If this changes the request shape of a tool, the graph or the prompts, run the relevant
steps in [VERIFY.md](../VERIFY.md) and say what you saw.

- [ ] Not applicable
- [ ] Ran: <!-- which steps, and the result -->

## Evals

- [ ] Not applicable
- [ ] `python -m scripts.eval --compare` — what moved:
