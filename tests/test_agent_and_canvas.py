from __future__ import annotations

from studylink import store
from studylink.agent import (
    WorkSessionAgent,
    citation_coverage,
    extract_citations,
    format_note_context,
    resolve_citations,
)
from studylink.canvas import strip_html


# ------------------------------------------------------------------ citations


def test_extract_citations_deduplicates_and_preserves_order():
    text = "Start with [N7], then [N3], and back to [N7]."
    assert extract_citations(text) == [7, 3]


def test_extract_citations_on_uncited_text():
    assert extract_citations("No citations at all here.") == []


def test_citation_coverage_flags_ids_that_were_never_supplied():
    coverage = citation_coverage("Point one [N1]. Point two [N42].", offered_note_ids=[1, 2])
    assert coverage["cited"] == [1, 42]
    assert coverage["hallucinated_ids"] == [42]
    assert coverage["unused_offered"] == [2]


def test_citation_coverage_clean_output():
    coverage = citation_coverage("All grounded [N1] and [N2].", offered_note_ids=[1, 2])
    assert coverage["hallucinated_ids"] == []
    assert coverage["unused_offered"] == []
    assert coverage["citation_count"] == 2


def test_resolve_citations_marks_unknown_notes(corpus):
    resolved = resolve_citations(corpus["conn"], [corpus["gradient_note"], 9999])
    assert resolved[0]["valid"] is True
    assert resolved[1]["valid"] is False


# ---------------------------------------------------------------- agent wiring


def test_prompt_includes_assignment_and_note_ids(corpus):
    assignment = store.get_assignment(corpus["conn"], corpus["gradient_assignment"])
    matches = corpus["retriever"].notes_for_assignment(assignment, apply_threshold=False)
    agent = WorkSessionAgent(corpus["conn"], corpus["retriever"])

    prompt = agent.build_initial_prompt(assignment, matches, "outline")
    assert assignment.name in prompt
    assert f"[N{corpus['gradient_note']}]" in prompt
    assert "RETRIEVED NOTES" in prompt


def test_note_context_is_truncated_but_labelled(corpus):
    assignment = store.get_assignment(corpus["conn"], corpus["gradient_assignment"])
    matches = corpus["retriever"].notes_for_assignment(assignment, apply_threshold=False)

    context = format_note_context(matches, max_chars=50)
    assert "truncated" in context
    assert f"[N{corpus['gradient_note']}]" in context


def test_note_context_with_no_matches():
    assert "no notes matched" in format_note_context([])


def test_search_tool_returns_citable_ids(corpus):
    agent = WorkSessionAgent(corpus["conn"], corpus["retriever"])
    result = agent._run_search({"query": "learning rate convergence", "top_k": 2})
    assert f"[N{corpus['gradient_note']}]" in result


def test_search_tool_handles_an_empty_query(corpus):
    agent = WorkSessionAgent(corpus["conn"], corpus["retriever"])
    assert "No query" in agent._run_search({"query": "  "})


# ---------------------------------------------------------------------- canvas


def test_strip_html_preserves_paragraph_structure():
    html = "<p>First para.</p><p>Second para.</p>"
    assert strip_html(html) == "First para.\n\nSecond para."


def test_strip_html_converts_lists_and_breaks():
    html = "<ul><li>Alpha</li><li>Beta</li></ul>"
    text = strip_html(html)
    assert "- Alpha" in text and "- Beta" in text


def test_strip_html_unescapes_entities():
    assert strip_html("<p>Tom &amp; Jerry &lt;3</p>") == "Tom & Jerry <3"


def test_strip_html_on_empty_input():
    assert strip_html("") == ""
    assert strip_html(None) == ""
