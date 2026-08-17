"""
The baseline: what the set scored last time it was accepted, and what changed since.

A run on its own is a number with nothing to be better or worse than. The baseline is a
committed run that says "this is the standard", and every later run is read as a delta
against it.

## What counts as a regression

Only two things, and both are deliberate:

- **A case that passed and now fails.** Per case, by id, which is why ids are stable and
  duplicates are rejected in `cases.py`. This is the check that matters - an aggregate can
  hide one case breaking behind another improving.
- **An aggregate metric that fell by more than `TOLERANCE`.** Retrieval metrics move
  slightly on re-ingestion and model calls are not deterministic, so an exact comparison
  would cry wolf on every run and get ignored, which is the failure mode a regression
  gate actually dies of.

Deliberately **not** regressions: a new case that fails (it was never passing, and adding
a known-failing case is a normal way to record a bug), a case that disappeared, and cost
or latency moving. Cost is reported because it is worth watching, not gated because a
cheaper wrong answer is not an improvement.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.evals.runner import Run

EVALS_DIR = Path(__file__).resolve().parent.parent.parent / "evals"
DEFAULT_BASELINE = EVALS_DIR / "baseline.json"
RUNS_DIR = EVALS_DIR / "runs"

# How far an aggregate may fall before it is called a regression. Retrieval is stable
# enough that anything past this is a real change, and the model is not deterministic
# enough for a tighter bound to mean anything.
TOLERANCE = 0.05

# Compared because they measure quality. Cost and latency are reported instead: a run that
# got cheaper by answering worse must not be able to pass.
GATED_METRICS = ("pass_rate", "recall_at_k", "precision_at_k", "mrr")


@dataclass
class Comparison:
    """The difference between a run and the baseline it is judged against."""

    regressions: list[str] = field(default_factory=list)
    fixes: list[str] = field(default_factory=list)
    new_cases: list[str] = field(default_factory=list)
    dropped_cases: list[str] = field(default_factory=list)
    # metric -> (baseline, current, delta)
    metrics: dict[str, tuple[float, float, float]] = field(default_factory=dict)
    metric_regressions: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.regressions and not self.metric_regressions


def save_run(run: Run, directory: Path | None = None) -> Path:
    """Write a run to the history, named for when it happened."""
    directory = directory or RUNS_DIR
    directory.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"{stamp}.json"
    path.write_text(run.model_dump_json(indent=2), encoding="utf-8")

    return path


def save_baseline(run: Run, path: Path | None = None) -> Path:
    """Accept a run as the standard. Deliberately explicit - never done automatically."""
    path = path or DEFAULT_BASELINE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(run.model_dump_json(indent=2), encoding="utf-8")

    return path


def load_baseline(path: Path | None = None) -> Run | None:
    """The accepted run, or None when there is not one yet."""
    path = path or DEFAULT_BASELINE

    if not path.exists():
        return None

    return Run(**json.loads(path.read_text(encoding="utf-8")))


def compare(baseline: Run, current: Run) -> Comparison:
    """
    Read the current run as a delta against the accepted one.

    Aggregates are computed over the cases **both runs contain**, not over each run whole.
    An average across two different case sets is not a comparison: adding one known-failing
    case would drop `pass_rate` and trip the gate, which would mean the only way to record
    a new bug is to break the build. Restricted to the shared ids, a metric moving is a
    statement about the same questions being answered differently.
    """
    was = {c.id: c for c in baseline.cases}
    now = {c.id: c for c in current.cases}
    shared = was.keys() & now.keys()

    comparison = Comparison(
        regressions=sorted(i for i in shared if was[i].passed and not now[i].passed),
        fixes=sorted(i for i in shared if not was[i].passed and now[i].passed),
        new_cases=sorted(now.keys() - was.keys()),
        dropped_cases=sorted(was.keys() - now.keys()),
    )

    order = sorted(shared)
    before = baseline.model_copy(update={"cases": [was[i] for i in order]}).summary()
    after = current.model_copy(update={"cases": [now[i] for i in order]}).summary()

    for metric in GATED_METRICS:
        old, new = float(before[metric]), float(after[metric])
        comparison.metrics[metric] = (old, new, new - old)
        if new < old - TOLERANCE:
            comparison.metric_regressions.append(metric)

    return comparison
