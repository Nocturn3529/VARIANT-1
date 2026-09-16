"""Pure display helpers in chat_pipeline."""

from __future__ import annotations

from chat_pipeline import (
    _SPOKEN_MAX_CHARS,
    spoken_lead,
)


# ---- spoken_lead: long reports are shown in full, voiced in part ----

def test_short_reply_spoken_whole():
    assert spoken_lead("The RTX 4060 wins on efficiency.") == "The RTX 4060 wins on efficiency."


def test_long_report_cut_at_sentence_boundary():
    report = ("The RTX 4060 is the better buy. " * 10          # ~320 chars of prose
              + "It draws 115W versus 165W for the RX 7600. " * 20)
    lead = spoken_lead(report)
    assert len(lead) <= _SPOKEN_MAX_CHARS
    assert lead.endswith(".")            # sentence boundary, not mid-word


def test_long_report_prefers_paragraph_break():
    report = "Verdict: buy the RTX 4060 for 1080p." + " More detail." * 8 + "\n\n" + "x" * 900
    lead = spoken_lead(report)
    assert lead.startswith("Verdict:")
    assert "x" not in lead
