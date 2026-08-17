"""
The metrics. Pure functions over already-collected results: no database, no model, no
network, so the arithmetic can be tested exhaustively and cheaply.

## What the retrieval numbers mean here

Relevance is judged at **section** granularity (see `evals/cases.yaml`), which is coarse.
The consequence is worth stating rather than discovering from a suspiciously good number:

- **recall@k** saturates. `Item fields` spans many chunks, so retrieving any one of them
  scores a full hit. Useful as a floor - a recall below 1.0 means the right *section* never
  came back at all, which is a real failure - and close to meaningless above it.
- **MRR** is the discriminating one. It asks where the first relevant chunk ranked, and
  the model reads the top of the list first, so 1.0 and 0.33 are genuinely different
  answers to "did retrieval work" even when recall calls both of them perfect.
- **precision@k** is the honest counterweight to recall. Five chunks of which one is
  relevant scores 0.2, and that is the number that moves when chunking gets better.

None of the three knows whether the retrieved text actually *answers* the question - only
that it came from the right part of the document. That gap is what `must_contain` on the
generated answer is for.
"""

from dataclasses import dataclass, field


@dataclass
class RetrievalScore:
    """How well the ranking did on one question."""

    recall_at_k: float
    precision_at_k: float
    mrr: float
    # 1-based ranks of every relevant chunk, so a report can show *where* they landed
    # rather than only how many there were.
    ranks: list[int] = field(default_factory=list)
    retrieved: int = 0

    @property
    def found(self) -> bool:
        return bool(self.ranks)


def score_retrieval(expected: list[str], retrieved_sections: list[str]) -> RetrievalScore:
    """
    Score one ranking against the sections that should have produced it.

    `retrieved_sections` is in rank order, one entry per returned chunk, and may contain
    None for a chunk the ingester could not attribute to a section - those simply never
    match, which is the correct reading of "we do not know where this came from".
    """
    wanted = set(expected)
    ranks = [
        rank
        for rank, section in enumerate(retrieved_sections, start=1)
        if section in wanted
    ]

    hit_sections = {s for s in retrieved_sections if s in wanted}
    k = len(retrieved_sections)

    return RetrievalScore(
        # Over *sections found*, not chunks: retrieving four chunks of the one expected
        # section is not four fifths of a recall, it is one section found.
        recall_at_k=len(hit_sections) / len(wanted) if wanted else 0.0,
        precision_at_k=len(ranks) / k if k else 0.0,
        # 0.0 rather than None when nothing was found, so a mean over cases stays defined
        # and a total miss drags the average down instead of vanishing from it.
        mrr=1 / ranks[0] if ranks else 0.0,
        ranks=ranks,
        retrieved=k,
    )


@dataclass
class AnswerScore:
    """Whether the turn did what the case says it should have."""

    answered: bool
    missing_tools: list[str] = field(default_factory=list)
    unexpected_tools: list[str] = field(default_factory=list)
    missing_phrases: list[str] = field(default_factory=list)
    missing_sources: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        # `unexpected_tools` is recorded but not failed: calling an extra tool is being
        # thorough, not being wrong, and a set that punishes it would push the prompt
        # towards doing less rather than towards being right.
        return (
            self.answered
            and not self.missing_tools
            and not self.missing_phrases
            and not self.missing_sources
        )


def score_answer(
    *,
    answer: str,
    tools_called: list[str],
    sources: list[str],
    expect_tools: list[str],
    must_contain: list[str],
    expect_sources: list[str],
) -> AnswerScore:
    """
    Grade one answer against a case's expectations.

    Phrases are matched case-insensitively against the raw answer. That is deliberately a
    low bar: it catches an answer that omitted a required field name or invented a
    different media type, and it does not pretend to judge prose. Anything finer either
    over-fits to wording or needs a model to grade it, and a grader that costs money per
    run is a grader people stop running.
    """
    lowered = answer.lower()
    called = set(tools_called)
    cited = set(sources)

    return AnswerScore(
        answered=bool(answer.strip()),
        missing_tools=[t for t in expect_tools if t not in called],
        unexpected_tools=sorted(called - set(expect_tools)) if expect_tools else [],
        missing_phrases=[p for p in must_contain if p.lower() not in lowered],
        missing_sources=[s for s in expect_sources if s not in cited],
    )


def mean(values: list[float]) -> float:
    """Zero for an empty set, so an aggregate never has to be None."""
    return sum(values) / len(values) if values else 0.0
