"""
The eval harness CLI.

Deliberately a script and not a pytest file. `pytest` in this project is the offline
suite - no network, no credentials, no database - and it has to stay that way to be worth
running on every change. This needs all three, costs money, and takes minutes. Those are
different tools and mixing them makes the fast one slow and the slow one skipped.

    # Cheap: embeddings and pgvector only, no model calls at all.
    python -m scripts.eval --retrieval-only

    # Everything, including live model and catalog calls.
    python -m scripts.eval

    # Are the live services actually up? No cases, no spend.
    python -m scripts.eval --smoke

    # Accept the current scores as the standard.
    python -m scripts.eval --save-baseline

    # Judge against the standard. Exits 1 on a regression, for CI.
    python -m scripts.eval --compare

Filters compose with all of the above:

    python -m scripts.eval --tag docs
    python -m scripts.eval --case item-required-fields --case item-media-type
"""

import argparse
import sys

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.db.models import DocChunk
from app.db.session import SessionLocal
from app.evals.baseline import (
    TOLERANCE,
    compare,
    load_baseline,
    save_baseline,
    save_run,
)
from app.evals.cases import Case, load_cases, select_cases
from app.evals.runner import CaseResult, run_cases

SEPARATOR = "=" * 78


def report_case(case: Case, result: CaseResult) -> None:
    """One line per case as it finishes, then the detail of whatever went wrong."""
    mark = "FAIL" if not result.passed else "pass"
    if result.error:
        mark = "ERROR"

    parts = []
    if result.mrr is not None:
        parts.append(f"mrr {result.mrr:.2f}  recall {result.recall_at_k:.2f}")
    if result.cost_usd is not None:
        parts.append(f"{result.steps} steps  ${result.cost_usd:.4f}  {result.ms}ms")

    # Flushed, because a full run is minutes of live model calls and Python buffers stdout
    # when it is piped or redirected - which is exactly when someone is watching a log to
    # see whether it is still moving.
    print(f"  [{mark:>5}] {case.id:<24} {'  |  '.join(parts)}", flush=True)

    if result.error:
        print(f"          {result.error}", flush=True)
        return

    # Only the misses, and named: "failed" without saying what was missing sends whoever
    # reads this back to run it again by hand.
    for label, missing in (
        ("expected tools not called", result.missing_tools),
        ("phrases missing from the answer", result.missing_phrases),
        ("sources not cited", result.missing_sources),
    ):
        if missing:
            print(f"          {label}: {', '.join(missing)}", flush=True)

    if result.mrr is not None and not result.ranks:
        print(
            f"          no chunk from {case.sections} in the top "
            f"{len(result.retrieved_sections)}; got {result.retrieved_sections}",
            flush=True,
        )


def smoke() -> int:
    """
    Are the live services reachable and behaving? No cases, no model tokens.

    The offline suite cannot see a catalog that changed its contract, a Bedrock region
    that lost model access, or an empty database. This is the five-second check that says
    which of those is true before a failing eval gets blamed on the prompt.
    """
    print(f"{SEPARATOR}\nSmoke checks\n{SEPARATOR}")
    failures = 0

    def check(name: str, fn) -> None:
        nonlocal failures
        try:
            print(f"  [ ok ] {name}: {fn()}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"  [FAIL] {name}: {type(e).__name__}: {e}")

    def database() -> str:
        db = SessionLocal()
        try:
            indexed = db.scalar(select(func.count(DocChunk.id)))
            if not indexed:
                raise RuntimeError("no chunks indexed - run app.rag.ingest first")
            return f"{indexed} chunks indexed"
        finally:
            db.close()

    def embeddings() -> str:
        from app.rag.embeddings import embed_text

        vector = embed_text("smoke test")
        if len(vector) != settings.embedding_dim:
            raise RuntimeError(
                f"embedding dimension {len(vector)} != configured {settings.embedding_dim}"
            )
        return f"{settings.embedding_model}, dim {len(vector)}"

    def catalog() -> str:
        from app.tools.stac_search import stac_search

        result = stac_search(
            [12.35, 41.75, 12.65, 42.0],
            datetime="2024-01-01/2024-01-31",
            collections=["sentinel-2-l2a"],
            limit=1,
        )
        if not result.count:
            raise RuntimeError("catalog returned no scenes for a search that should match")
        return f"{settings.stac_api_url}, {result.count} scene(s)"

    def model() -> str:
        # The Models API costs no tokens, so this proves reachability, credentials and
        # that the configured model exists - without buying a completion to find out.
        from app.agents.graph import _client

        return _client().models.retrieve(settings.claude_model).id

    check("database", database)
    check("embeddings (Bedrock)", embeddings)
    check("STAC catalog", catalog)
    check("model (Anthropic)", model)

    print()
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the eval set")
    parser.add_argument("--retrieval-only", action="store_true", help="No model calls")
    parser.add_argument("--smoke", action="store_true", help="Check live services and exit")
    parser.add_argument("--top-k", type=int, default=5, help="Chunks retrieved per question")
    parser.add_argument("--tag", action="append", metavar="TAG", help="Only cases with this tag")
    parser.add_argument("--case", action="append", metavar="ID", help="Only these case ids")
    parser.add_argument("--save-baseline", action="store_true", help="Accept this run")
    parser.add_argument("--compare", action="store_true", help="Judge against the baseline")
    parser.add_argument("--no-save", action="store_true", help="Do not write to evals/runs/")
    args = parser.parse_args()

    if args.smoke:
        return smoke()

    try:
        cases = select_cases(load_cases(), tags=args.tag, ids=args.case)
    except (FileNotFoundError, TypeError, ValueError) as e:
        print(f"{e}", file=sys.stderr)
        return 1

    if not cases:
        print("No cases matched.", file=sys.stderr)
        return 1

    gradeable = [c for c in cases if c.grades_retrieval] if args.retrieval_only else cases
    live = sum(1 for c in gradeable if c.grades_answer and not args.retrieval_only)

    print(f"{SEPARATOR}")
    print(f"Cases: {len(gradeable)}, top-k: {args.top_k}")
    print(f"Model: {settings.claude_model}  |  Embeddings: {settings.embedding_model}")
    # Said plainly and up front: this is the flag that turns a free run into a paid one.
    print(
        "Model calls: none (retrieval only)"
        if args.retrieval_only
        else f"Model calls: {live} case(s) will run live turns and cost money"
    )
    print(SEPARATOR, flush=True)

    db = SessionLocal()
    try:
        run = run_cases(
            db,
            gradeable,
            top_k=args.top_k,
            retrieval_only=args.retrieval_only,
            on_result=report_case,
        )
    except SQLAlchemyError as e:
        print(f"\nDatabase error ({settings.database_url}): {e}", file=sys.stderr)
        return 1
    finally:
        db.close()

    summary = run.summary()
    print(f"\n{SEPARATOR}")
    print(
        f"{summary['passed']}/{summary['cases']} passed"
        + (f", {summary['errors']} errored" if summary["errors"] else "")
    )
    print(
        f"recall@{args.top_k} {summary['recall_at_k']:.3f}  |  "
        f"precision@{args.top_k} {summary['precision_at_k']:.3f}  |  "
        f"MRR {summary['mrr']:.3f}"
    )
    if not args.retrieval_only:
        print(f"cost ${summary['cost_usd']:.4f}  |  {summary['ms'] / 1000:.1f}s")
    print(SEPARATOR)

    if not args.no_save:
        print(f"\nRun saved to {save_run(run)}")

    exit_code = 0

    if args.compare:
        baseline = load_baseline()
        if baseline is None:
            print("\nNo baseline yet - run with --save-baseline to set one.", file=sys.stderr)
            return 1
        exit_code = report_comparison(baseline, run)

    if args.save_baseline:
        print(f"Baseline written to {save_baseline(run)}")

    return exit_code


def report_comparison(baseline, run) -> int:
    """Print the delta against the baseline and return the exit code for CI."""
    result = compare(baseline, run)

    print(f"\nAgainst the baseline of {baseline.created_at}:")

    for metric, (old, new, delta) in result.metrics.items():
        flag = "  <-- regression" if metric in result.metric_regressions else ""
        print(f"  {metric:<16} {old:.3f} -> {new:.3f}  ({delta:+.3f}){flag}")

    for label, ids in (
        ("regressed", result.regressions),
        ("fixed", result.fixes),
        ("new", result.new_cases),
        ("dropped", result.dropped_cases),
    ):
        if ids:
            print(f"  {label}: {', '.join(ids)}")

    if result.ok:
        print("\nNo regressions.")
        return 0

    print(f"\nREGRESSION (metrics may fall by at most {TOLERANCE}).", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
