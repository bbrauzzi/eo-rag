"""
The labelled set: what it looks like, and where it is read from.

## Why a file and not the `eval_cases` table

`app/db/models.py` has carried an `EvalCase` table since step 0, and this step does not
fill it in. A labelled set is a stated opinion about what a correct answer is, which means
its natural home is wherever opinions get reviewed: a YAML file diffs in a pull request,
travels with the code that it grades, and can be edited without a database. A row cannot
do any of those.

The table is left in place rather than dropped. Dropping it is a one-line Alembic
migration now that `alembic/` exists - it wasn't when this reasoning was first written,
back when `scripts/init_db.sql` only ran on a data volume's first creation and removing
a table meant a manual `ALTER` on every existing database. That's no longer the blocker;
schema cleanup is just out of scope for this file. It is dead schema, and ROADMAP.md
says so.

## Two kinds of case in one file

A case carrying `sections` is gradeable on **retrieval**: it names where in the corpus the
answer lives. A case carrying `expect_tools` or `must_contain` is gradeable on the
**answer**. Most documentation cases are both; the catalog cases are only the second,
because retrieval is not what they are testing.

That split is what makes `--retrieval-only` meaningful: it runs the cases that can be
graded without spending a single model call.
"""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

# The repository's own set. Overridable on the CLI, because a labelled set for a different
# corpus is a different file rather than a different code path.
DEFAULT_CASES = Path(__file__).resolve().parent.parent.parent / "evals" / "cases.yaml"


class Case(BaseModel):
    """
    One labelled question.

    Named `Case` rather than `EvalCase` so that it cannot be confused with the unused
    SQLAlchemy model of that name - they are not two representations of one thing.
    """

    id: str
    question: str

    # Retrieval ground truth: the sections of the corpus that contain the answer.
    sections: list[str] = Field(default_factory=list)

    # Answer expectations. `expect_tools` is a subset check - a model that calls an extra
    # tool has not failed, it has been thorough, and the extras are recorded either way.
    expect_tools: list[str] = Field(default_factory=list)
    must_contain: list[str] = Field(default_factory=list)
    expect_sources: list[str] = Field(default_factory=list)

    tags: list[str] = Field(default_factory=list)
    note: str | None = None

    @property
    def grades_retrieval(self) -> bool:
        return bool(self.sections)

    @property
    def grades_answer(self) -> bool:
        return bool(self.expect_tools or self.must_contain or self.expect_sources)


def load_cases(path: Path | None = None) -> list[Case]:
    """
    Read and validate the labelled set.

    Duplicate ids are rejected rather than tolerated: the baseline is keyed on the id, so
    two cases sharing one would silently compare a case against a different case's history
    - a regression report that is wrong is worse than no regression report.
    """
    path = path or DEFAULT_CASES

    if not path.exists():
        raise FileNotFoundError(f"No eval cases at {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    if not isinstance(raw, list):
        raise TypeError(f"{path} must hold a list of cases, got {type(raw).__name__}")

    cases = [Case(**entry) for entry in raw]

    seen: set[str] = set()
    duplicates = sorted({c.id for c in cases if c.id in seen or seen.add(c.id)})
    if duplicates:
        raise ValueError(f"Duplicate case ids in {path}: {', '.join(duplicates)}")

    return cases


def select_cases(
    cases: list[Case], tags: list[str] | None = None, ids: list[str] | None = None
) -> list[Case]:
    """Filter by tag or by id, in the order the file declares them."""
    if ids:
        wanted = set(ids)
        cases = [c for c in cases if c.id in wanted]
    if tags:
        tagged = set(tags)
        cases = [c for c in cases if tagged & set(c.tags)]

    return cases
