"""
Tests for the eval harness.

Fully offline, like the rest of the suite: the harness *runs* against live services, but
everything that decides what a score means is pure and is tested here. A metric nobody
checked is a number, not a measurement - and this is the code that will be used to decide
whether a change made retrieval better, so it has to be right before it is trusted.

`scripts/eval.py` itself is not tested here. It is argument parsing and printing over
these functions, and the roadmap keeps it out of pytest on purpose.
"""

import json
from pathlib import Path

import pytest

from app.evals.baseline import (
    TOLERANCE,
    compare,
    load_baseline,
    save_baseline,
    save_run,
)
from app.evals.cases import Case, load_cases, select_cases
from app.evals.runner import CaseResult, Run
from app.evals.scoring import mean, score_answer, score_retrieval


def run_with(cases: list[CaseResult], **kwargs) -> Run:
    return Run(
        created_at="2026-01-01T00:00:00+00:00",
        model="claude-sonnet-4-6",
        embedding_model="amazon.titan-embed-text-v2:0",
        top_k=5,
        retrieval_only=False,
        cases=cases,
        **kwargs,
    )


def case_result(id: str, passed: bool, **kwargs) -> CaseResult:
    return CaseResult(id=id, passed=passed, **kwargs)


# --- retrieval metrics --------------------------------------------------------


def test_a_perfect_ranking_scores_one_everywhere():
    score = score_retrieval(["Item fields"], ["Item fields"])

    assert (score.recall_at_k, score.precision_at_k, score.mrr) == (1.0, 1.0, 1.0)
    assert score.ranks == [1]


def test_nothing_relevant_scores_zero_rather_than_none():
    """A total miss has to drag an average down, not vanish from it."""
    score = score_retrieval(["Item fields"], ["Extensions", "Foundations"])

    assert (score.recall_at_k, score.precision_at_k, score.mrr) == (0.0, 0.0, 0.0)
    assert score.found is False


def test_mrr_is_the_reciprocal_of_the_first_relevant_rank():
    """The discriminating metric: the model reads the top of the list first."""
    score = score_retrieval(["Item fields"], ["Extensions", "Overview", "Item fields"])

    assert score.mrr == pytest.approx(1 / 3)
    assert score.ranks == [3]


def test_recall_saturates_where_mrr_still_discriminates():
    """
    The limitation of section-level labels, asserted rather than left in a docstring.
    Both of these found the section; only one of them put it first.
    """
    first = score_retrieval(["Item fields"], ["Item fields", "Extensions", "Overview"])
    last = score_retrieval(["Item fields"], ["Extensions", "Overview", "Item fields"])

    assert first.recall_at_k == last.recall_at_k == 1.0
    assert first.mrr > last.mrr


def test_recall_counts_sections_found_not_chunks_returned():
    """
    Four chunks of the one expected section is one section found, not four fifths of a
    recall - otherwise a chunker that split a section finer would look like an improvement.
    """
    score = score_retrieval(["Item fields"], ["Item fields"] * 4 + ["Overview"])

    assert score.recall_at_k == 1.0
    assert score.precision_at_k == pytest.approx(0.8)


def test_recall_over_several_expected_sections_is_a_fraction():
    score = score_retrieval(
        ["Item fields", "Catalog fields"], ["Item fields", "Overview", "Extensions"]
    )

    assert score.recall_at_k == pytest.approx(0.5)


def test_precision_is_the_honest_counterweight_to_recall():
    """One relevant chunk out of five is 0.2, and that is the number chunking moves."""
    score = score_retrieval(["Item fields"], ["Item fields", "a", "b", "c", "d"])

    assert score.recall_at_k == 1.0
    assert score.precision_at_k == pytest.approx(0.2)


def test_a_chunk_with_no_section_never_matches():
    """The correct reading of "we do not know where this came from"."""
    score = score_retrieval(["Item fields"], [None, None])

    assert score.found is False
    assert score.retrieved == 2


def test_an_empty_ranking_does_not_divide_by_zero():
    score = score_retrieval(["Item fields"], [])

    assert (score.recall_at_k, score.precision_at_k, score.mrr) == (0.0, 0.0, 0.0)


def test_mean_of_nothing_is_zero():
    assert mean([]) == 0.0


# --- answer scoring -----------------------------------------------------------


def answer_score(**overrides):
    defaults = {
        "answer": "A STAC Item requires stac_version, geometry, properties and assets.",
        "tools_called": ["rag_lookup"],
        "sources": ["stac-spec-core"],
        "expect_tools": ["rag_lookup"],
        "must_contain": ["stac_version"],
        "expect_sources": ["stac-spec-core"],
    }
    return score_answer(**{**defaults, **overrides})


def test_a_good_answer_passes():
    assert answer_score().passed is True


def test_a_missing_phrase_fails_and_is_named():
    score = answer_score(must_contain=["stac_version", "collection"])

    assert score.passed is False
    assert score.missing_phrases == ["collection"]


def test_phrases_are_matched_case_insensitively():
    """The corpus writes REQUIRED and the model writes required; neither is a failure."""
    score = answer_score(answer="It is REQUIRED.", must_contain=["required"])

    assert score.missing_phrases == []


def test_a_tool_that_was_not_called_fails_the_case():
    score = answer_score(tools_called=["rag_lookup"], expect_tools=["rag_lookup", "stac_search"])

    assert score.passed is False
    assert score.missing_tools == ["stac_search"]


def test_an_extra_tool_is_recorded_but_does_not_fail():
    """
    Being thorough is not being wrong, and failing it would push the prompt towards doing
    less rather than towards being right.
    """
    score = answer_score(tools_called=["rag_lookup", "stac_search"])

    assert score.passed is True
    assert score.unexpected_tools == ["stac_search"]


def test_an_uncited_source_fails():
    score = answer_score(sources=[])

    assert score.passed is False
    assert score.missing_sources == ["stac-spec-core"]


def test_an_empty_answer_fails_whatever_else_happened():
    """A turn that produced no text has not answered, however many tools it ran."""
    score = answer_score(answer="   ", must_contain=[], expect_sources=[])

    assert score.answered is False
    assert score.passed is False


# --- the labelled set ---------------------------------------------------------


def test_the_repository_set_loads_and_validates():
    """The file ships with the code, so a typo in it is a broken build, not a surprise."""
    cases = load_cases()

    assert cases
    assert all(c.id and c.question for c in cases)


def test_every_case_grades_something():
    """A case with no sections and no expectations would silently always pass."""
    for case in load_cases():
        assert case.grades_retrieval or case.grades_answer, case.id


def test_labelled_sections_exist_in_the_corpus():
    """
    Guards the one mistake that makes retrieval look broken when it is not: a section
    label no chunk can ever carry, because the heading was renamed or never existed.

    Read from the markdown rather than the database, so it holds in the offline suite -
    the labels are a claim about the corpus, and the corpus is a file in the repository.
    """
    corpus = Path(__file__).resolve().parent.parent / "data" / "stac-spec-core.md"

    # `##` is what the chunker records as `section`, with the `#` title as the fallback,
    # so both levels are legitimate labels and anything deeper is not.
    known = {
        line.lstrip("#").strip()
        for line in corpus.read_text(encoding="utf-8").splitlines()
        if line.startswith(("# ", "## "))
    }

    for case in load_cases():
        for section in case.sections:
            assert section in known, f"{case.id}: no heading '{section}' in the corpus"


def test_duplicate_ids_are_rejected(tmp_path):
    """The baseline is keyed on the id, so a duplicate compares a case to another case."""
    path = tmp_path / "cases.yaml"
    path.write_text(
        "- id: same\n  question: a\n  sections: [x]\n"
        "- id: same\n  question: b\n  sections: [y]\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Duplicate case ids"):
        load_cases(path)


def test_a_missing_file_says_so(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_cases(tmp_path / "nope.yaml")


def test_cases_can_be_filtered_by_tag_and_id():
    cases = [
        Case(id="a", question="q", tags=["docs"]),
        Case(id="b", question="q", tags=["catalog"]),
        Case(id="c", question="q", tags=["docs", "catalog"]),
    ]

    assert [c.id for c in select_cases(cases, tags=["docs"])] == ["a", "c"]
    assert [c.id for c in select_cases(cases, ids=["b"])] == ["b"]


def test_filtering_keeps_the_order_the_file_declares():
    """So a report reads the same way every time, whatever the filter."""
    cases = [Case(id=i, question="q", tags=["docs"]) for i in ("z", "m", "a")]

    assert [c.id for c in select_cases(cases, tags=["docs"])] == ["z", "m", "a"]


# --- the run record -----------------------------------------------------------


def test_the_summary_averages_only_what_was_graded():
    """A case that ran no retrieval must not be averaged in as a zero."""
    run = run_with(
        [
            case_result("a", True, mrr=1.0, recall_at_k=1.0, precision_at_k=0.2),
            case_result("b", True),  # answer-only case: no retrieval metrics
        ]
    )

    assert run.summary()["mrr"] == pytest.approx(1.0)
    assert run.summary()["pass_rate"] == pytest.approx(1.0)


def test_the_summary_counts_errors_separately_from_failures():
    run = run_with(
        [
            case_result("a", True),
            case_result("b", False),
            case_result("c", False, error="RuntimeError: catalog unreachable"),
        ]
    )

    summary = run.summary()
    assert (summary["cases"], summary["passed"], summary["errors"]) == (3, 1, 1)


def test_cost_and_latency_add_up_over_the_run():
    run = run_with(
        [
            case_result("a", True, answered=True, cost_usd=0.01, ms=1000),
            case_result("b", True, answered=True, cost_usd=0.02, ms=2000),
        ]
    )

    assert run.summary()["cost_usd"] == pytest.approx(0.03)
    assert run.summary()["ms"] == 3000


# --- baseline and regression --------------------------------------------------


def test_a_case_that_passed_and_now_fails_is_a_regression():
    before = run_with([case_result("a", True)])
    after = run_with([case_result("a", False)])

    result = compare(before, after)

    assert result.regressions == ["a"]
    assert result.ok is False


def test_a_case_that_was_failing_and_now_passes_is_a_fix():
    result = compare(run_with([case_result("a", False)]), run_with([case_result("a", True)]))

    assert result.fixes == ["a"]
    assert result.ok is True


def test_a_new_failing_case_is_not_a_regression():
    """
    Adding a known-failing case is how a bug gets recorded. Gating on it would mean the
    only way to write a failing test is to break the build.
    """
    before = run_with([case_result("a", True)])
    after = run_with([case_result("a", True), case_result("b", False)])

    result = compare(before, after)

    assert result.new_cases == ["b"]
    assert result.regressions == []
    assert result.ok is True


def test_aggregates_are_compared_over_the_cases_both_runs_share():
    """
    An average across two different case sets is not a comparison. Without this, the new
    failing case above drags `pass_rate` from 1.0 to 0.5 and trips the gate on its own.
    """
    before = run_with([case_result("a", True, mrr=1.0)])
    after = run_with([case_result("a", True, mrr=1.0), case_result("b", False, mrr=0.0)])

    result = compare(before, after)

    # Case `a` scored the same in both runs, so nothing moved.
    assert result.metrics["mrr"] == (1.0, 1.0, 0.0)
    assert result.metrics["pass_rate"] == (1.0, 1.0, 0.0)
    assert result.ok is True


def test_a_removed_case_is_reported_not_failed():
    result = compare(
        run_with([case_result("a", True), case_result("b", True)]),
        run_with([case_result("a", True)]),
    )

    assert result.dropped_cases == ["b"]
    assert result.ok is True


def test_a_metric_falling_past_the_tolerance_is_a_regression():
    before = run_with([case_result("a", True, mrr=1.0, recall_at_k=1.0, precision_at_k=1.0)])
    after = run_with([case_result("a", True, mrr=0.5, recall_at_k=1.0, precision_at_k=1.0)])

    result = compare(before, after)

    assert "mrr" in result.metric_regressions
    assert result.ok is False


def test_a_metric_wobbling_inside_the_tolerance_is_not():
    """
    Re-ingestion moves these slightly and model calls are not deterministic. A gate that
    cries wolf every run is a gate that gets switched off.
    """
    before = run_with([case_result("a", True, mrr=1.0, recall_at_k=1.0, precision_at_k=1.0)])
    after = run_with(
        [case_result("a", True, mrr=1.0 - TOLERANCE / 2, recall_at_k=1.0, precision_at_k=1.0)]
    )

    assert compare(before, after).ok is True


def test_an_improving_metric_is_never_a_regression():
    before = run_with([case_result("a", True, mrr=0.5, recall_at_k=0.5, precision_at_k=0.5)])
    after = run_with([case_result("a", True, mrr=1.0, recall_at_k=1.0, precision_at_k=1.0)])

    result = compare(before, after)

    assert result.metric_regressions == []
    assert result.metrics["mrr"][2] == pytest.approx(0.5)


def test_cost_is_reported_but_never_gated():
    """A run that got cheaper by answering worse must not be able to pass on that."""
    from app.evals.baseline import GATED_METRICS

    assert "cost_usd" not in GATED_METRICS
    assert "ms" not in GATED_METRICS


def test_a_run_survives_the_round_trip_to_disk(tmp_path):
    """The baseline is only useful if it reads back as what was written."""
    run = run_with([case_result("a", True, mrr=0.5, answer="Items are Features.")])

    path = save_baseline(run, tmp_path / "baseline.json")
    restored = load_baseline(path)

    assert restored.cases[0].answer == "Items are Features."
    assert restored.summary() == run.summary()


def test_no_baseline_yet_is_none_rather_than_an_error(tmp_path):
    assert load_baseline(tmp_path / "absent.json") is None


def test_saving_a_run_names_it_for_when_it_happened(tmp_path):
    path = save_run(run_with([case_result("a", True)]), tmp_path)

    assert path.parent == tmp_path
    assert path.suffix == ".json"
    assert json.loads(path.read_text())["cases"][0]["id"] == "a"
