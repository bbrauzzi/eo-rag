"""
Running the labelled set against the real thing.

This is the module that costs money and needs the network, which is why everything it can
delegate lives elsewhere: the metrics are in `scoring.py` and are pure, the case list is
in `cases.py` and is data. What is left here is orchestration and the shape of a run.

## Why it drives `stream_answer` rather than `answer_question`

The tools a turn used are not on the `Answer` - `/ask` returns exactly
`{answer, sources, conversation_id}` and `tests/test_ask.py` pins that. They *are* on the
streaming event vocabulary, which reports every `tool_start` by name. So the harness
consumes the same events an SSE client would, in process, and gets tool calls, sources,
steps and the answer without a single change to the API's response shape.

The two entry points share the graph and `test_the_streaming_and_the_blocking_path_agree`
holds them to the same output, so this measures `/ask` as faithfully as it measures
`/ask/stream`.

## Every case gets its own conversation

A fresh `conversation_id` per case, for three reasons: cases must not see each other's
history, the per-conversation budget must not refuse case eleven because of cases one to
ten, and `conversation_spend` then prices exactly one case.
"""

import time
import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.agents.graph import conversation_spend, stream_answer
from app.config import settings
from app.evals.cases import Case
from app.evals.scoring import (
    AnswerScore,
    RetrievalScore,
    mean,
    score_answer,
    score_retrieval,
)
from app.rag.retrieval import retrieve_with_scores

DEFAULT_TOP_K = 5


class CaseResult(BaseModel):
    """
    What one case produced. Everything here is JSON-serializable on purpose: a run is
    written to disk and compared against a later one.
    """

    id: str
    passed: bool
    graded: list[str] = Field(default_factory=list)

    # Retrieval, absent when the case does not label any sections.
    recall_at_k: float | None = None
    precision_at_k: float | None = None
    mrr: float | None = None
    ranks: list[int] = Field(default_factory=list)
    retrieved_sections: list[str | None] = Field(default_factory=list)

    # The turn, absent when only retrieval was graded.
    answered: bool | None = None
    tools: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    steps: int | None = None
    cost_usd: float | None = None
    ms: int | None = None
    missing_tools: list[str] = Field(default_factory=list)
    unexpected_tools: list[str] = Field(default_factory=list)
    missing_phrases: list[str] = Field(default_factory=list)
    missing_sources: list[str] = Field(default_factory=list)

    # Kept whole rather than truncated: when a case regresses, the answer is the evidence,
    # and a run file that cannot tell you what was actually said is a scoreboard.
    answer: str = ""
    error: str | None = None


class Run(BaseModel):
    """One execution of the set, with enough context to know what it was measuring."""

    created_at: str
    model: str
    embedding_model: str
    top_k: int
    retrieval_only: bool
    cases: list[CaseResult] = Field(default_factory=list)

    def summary(self) -> dict:
        """The aggregates a baseline is compared on."""
        graded = [c for c in self.cases if c.error is None]

        # Each metric filters on its own field rather than on a shared "did retrieval
        # run" flag. The three do move together in practice, but a summary that assumes
        # so raises TypeError on the one record where they do not - and a run record read
        # back from a file is exactly where that assumption stops holding.
        def present(attr: str) -> list[float]:
            return [getattr(c, attr) for c in graded if getattr(c, attr) is not None]

        return {
            "cases": len(self.cases),
            "passed": sum(1 for c in self.cases if c.passed),
            "errors": sum(1 for c in self.cases if c.error),
            "pass_rate": mean([1.0 if c.passed else 0.0 for c in self.cases]),
            "recall_at_k": mean(present("recall_at_k")),
            "precision_at_k": mean(present("precision_at_k")),
            "mrr": mean(present("mrr")),
            "cost_usd": sum(c.cost_usd or 0.0 for c in graded if c.cost_usd is not None),
            "ms": sum(c.ms or 0 for c in graded if c.ms is not None),
        }


def _sections_for(db: Session, question: str, top_k: int) -> list[str | None]:
    """The sections of the top-k chunks, in rank order."""
    return [chunk.section for chunk, _ in retrieve_with_scores(db, question, top_k=top_k)]


def run_turn(db: Session, question: str) -> dict:
    """
    Ask one question and collect what the turn did, from the events it emits.

    Returns plain data rather than an object because the caller only ever destructures it,
    and the events themselves are the contract being read.
    """
    conversation_id = str(uuid.uuid4())
    tools: list[str] = []
    done: dict = {}
    started = time.perf_counter()

    for event in stream_answer(db, question, conversation_id):
        if event["type"] == "tool_start":
            tools.append(event["name"])
        elif event["type"] == "done":
            done = event

    _, cost = conversation_spend(conversation_id)

    return {
        "answer": done.get("answer", ""),
        "sources": done.get("sources", []),
        "steps": done.get("steps", 0),
        "tools": tools,
        "cost_usd": cost,
        "ms": round((time.perf_counter() - started) * 1000),
    }


def run_case(db: Session, case: Case, top_k: int, retrieval_only: bool) -> CaseResult:
    """
    Run one case as far as it is gradeable, and never raise.

    A case that blows up is recorded as a failure carrying its error, because one
    unreachable catalog must not throw away the other eleven results - and because "this
    case errored" is itself a finding a baseline should be able to regress on.
    """
    result = CaseResult(id=case.id, passed=False)

    try:
        retrieval: RetrievalScore | None = None
        if case.grades_retrieval:
            sections = _sections_for(db, case.question, top_k)
            retrieval = score_retrieval(case.sections, sections)
            result.graded.append("retrieval")
            result.recall_at_k = retrieval.recall_at_k
            result.precision_at_k = retrieval.precision_at_k
            result.mrr = retrieval.mrr
            result.ranks = retrieval.ranks
            result.retrieved_sections = sections

        answer: AnswerScore | None = None
        if case.grades_answer and not retrieval_only:
            turn = run_turn(db, case.question)
            answer = score_answer(
                answer=turn["answer"],
                tools_called=turn["tools"],
                sources=turn["sources"],
                expect_tools=case.expect_tools,
                must_contain=case.must_contain,
                expect_sources=case.expect_sources,
            )
            result.graded.append("answer")
            result.answered = answer.answered
            result.tools = turn["tools"]
            result.sources = turn["sources"]
            result.steps = turn["steps"]
            result.cost_usd = turn["cost_usd"]
            result.ms = turn["ms"]
            result.answer = turn["answer"]
            result.missing_tools = answer.missing_tools
            result.unexpected_tools = answer.unexpected_tools
            result.missing_phrases = answer.missing_phrases
            result.missing_sources = answer.missing_sources

        # A case is only as passed as the things actually graded. Retrieval passes when the
        # labelled section came back at all - how well it ranked is the metric's job, not
        # a pass/fail line invented here.
        checks = []
        if retrieval is not None:
            checks.append(retrieval.found)
        if answer is not None:
            checks.append(answer.passed)

        result.passed = bool(checks) and all(checks)
    except Exception as e:  # noqa: BLE001
        result.error = f"{type(e).__name__}: {e}"

    return result


def run_cases(
    db: Session,
    cases: list[Case],
    top_k: int = DEFAULT_TOP_K,
    retrieval_only: bool = False,
    on_result=None,
) -> Run:
    """Run the set. `on_result` is called per case so a CLI can report as it goes."""
    run = Run(
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        model=settings.claude_model,
        embedding_model=settings.embedding_model,
        top_k=top_k,
        retrieval_only=retrieval_only,
    )

    for case in cases:
        result = run_case(db, case, top_k, retrieval_only)
        run.cases.append(result)
        if on_result:
            on_result(case, result)

    return run
