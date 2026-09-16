"""tools_prompt compaction — the tool list is the biggest single block in a
desktop task's system prompt (measured live: 11.3k chars ≈ 3.5k tokens of a
12k local context). Large disclosures clip descriptions to their first
sentence; contract-carrying tools keep full text.
"""

from __future__ import annotations

import tools


def _spec(name, desc):
    return {"name": name, "description": desc, "params": {}}


_LONG = ("Does the thing. Second sentence with extra guidance the desktop "
         "guide already covers. Third sentence of examples and caveats.")


def test_small_disclosures_keep_full_descriptions():
    out = tools.tools_prompt([_spec("a_tool", _LONG)] * 3)
    assert "Third sentence of examples" in out


def test_large_disclosures_clip_to_first_sentence():
    specs = [_spec(f"tool_{i}", _LONG) for i in range(tools.TOOLS_COMPACT_MIN + 1)]
    out = tools.tools_prompt(specs)
    assert "Does the thing." in out
    assert "Second sentence" not in out


def test_contract_tools_keep_full_text_even_when_compacted():
    specs = [_spec(f"tool_{i}", _LONG) for i in range(tools.TOOLS_COMPACT_MIN)]
    specs.append(_spec("apply_patch", _LONG))
    out = tools.tools_prompt(specs)
    line = next(l for l in out.splitlines() if l.startswith("- apply_patch"))
    assert "Third sentence of examples" in line


def test_when_avoid_lines_appear_in_tools_prompt():
    specs = [{
        "name": "glob", "params": {},
        "description": "List or find files.",
        "when": "listing a directory",
        "avoid": "reading file contents",
    }]
    out = tools.tools_prompt(specs)
    assert "use when: listing a directory" in out
    assert "do not use when: reading file contents" in out


def test_compact_desc_clips_very_long_single_sentences_at_word_boundary():
    one_long = "word " * 60   # a single 300-char "sentence" with no period
    clipped = tools._compact_desc(one_long)
    assert len(clipped) <= 180
    assert clipped.endswith("…")
