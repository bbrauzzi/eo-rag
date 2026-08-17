"""Tests for markdown document splitting (app.rag.ingest.split_markdown)."""

import pytest

from app.rag.ingest import split_markdown


def test_empty_text_returns_no_chunks():
    assert split_markdown("") == []


def test_text_without_headers_has_no_section():
    chunks = split_markdown("Just a paragraph with no headers.")

    assert len(chunks) == 1
    assert chunks[0]["content"] == "Just a paragraph with no headers."
    assert chunks[0]["section"] is None


def test_splits_on_headers_and_keeps_them_in_the_content():
    text = (
        "# STAC Spec\n"
        "Introduction to the document.\n\n"
        "## Item\n"
        "An Item represents a single observation.\n\n"
        "## Collection\n"
        "A Collection groups homogeneous Items.\n"
    )

    chunks = split_markdown(text)

    assert [c["section"] for c in chunks] == ["STAC Spec", "Item", "Collection"]
    assert chunks[0]["content"].startswith("# STAC Spec")
    assert "An Item represents" in chunks[1]["content"]
    assert chunks[1]["content"].startswith("## Item")
    assert "A Collection groups" in chunks[2]["content"]


def test_section_falls_back_to_title_when_no_h2():
    chunks = split_markdown("# Title only\nBody of the document.\n")

    assert len(chunks) == 1
    assert chunks[0]["section"] == "Title only"


def test_subsection_inherits_the_parent_section():
    text = (
        "# Title\n"
        "## Section\n"
        "Text of the section.\n\n"
        "### Subsection\n"
        "Text of the subsection.\n"
    )

    chunks = split_markdown(text)

    assert [c["section"] for c in chunks] == ["Section", "Section"]
    assert "Text of the subsection." in chunks[-1]["content"]


def test_long_section_is_split_into_multiple_chunks():
    paragraph = " ".join(["word"] * 60)  # ~300 characters
    text = "## Long section\n" + "\n\n".join([paragraph] * 5)

    chunks = split_markdown(text)

    assert len(chunks) > 1
    # every chunk stays within the configured chunk_size and keeps its source section
    assert all(len(c["content"]) <= 800 for c in chunks)
    assert all(c["section"] == "Long section" for c in chunks)


def test_short_section_is_not_split():
    text = "## Short\nTwo lines only.\nNothing more.\n"

    chunks = split_markdown(text)

    assert len(chunks) == 1


def test_chunks_have_overlap_between_consecutive_pieces():
    paragraph = " ".join(f"w{i}" for i in range(400))
    chunks = split_markdown("## Overlap\n" + paragraph)

    assert len(chunks) > 1
    tail = chunks[0]["content"].split()[-1]
    assert tail in chunks[1]["content"]


def test_header_is_never_emitted_as_a_standalone_chunk():
    paragraph = " ".join(f"w{i}" for i in range(400))
    chunks = split_markdown("## Section\n" + paragraph)

    assert all(c["content"].strip() != "## Section" for c in chunks)
    # the header stays at the top of the first content chunk
    assert chunks[0]["content"].startswith("## Section\nw0 ")
    assert all(c["section"] == "Section" for c in chunks)


def test_section_without_body_is_dropped():
    text = "## Empty\n\n## Full\nActual content.\n"

    chunks = split_markdown(text)

    assert [c["section"] for c in chunks] == ["Full"]
    assert "Empty" not in chunks[0]["content"]


def test_document_made_only_of_headers_produces_no_chunks():
    assert split_markdown("# Title\n## Section\n### Subsection\n") == []


@pytest.mark.parametrize("text", ["", "   ", "\n\n"])
def test_blank_inputs_produce_no_content_chunks(text):
    assert all(c["content"].strip() for c in split_markdown(text))
