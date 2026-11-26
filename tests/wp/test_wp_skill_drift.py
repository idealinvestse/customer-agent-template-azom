"""Regression tests preventing drift between WP ops code, playbooks, docs, and skill cards."""

from __future__ import annotations

import re
from pathlib import Path

from ecom_ops.wp_ops.agent_tools import (
    _FILE_READ_DEFAULT,
    _FILE_READ_MAX,
    ALL_FILE_TOOLS,
    FILE_LOOP_MAX_CALLS,
    FILE_LOOP_MAX_READS,
    FILE_LOOP_MAX_RESULT_BYTES,
    FILE_LOOP_MAX_SEARCHES,
    FILE_LOOP_MAX_TOTAL_BYTES,
    WP_TOOL_NAMES,
)
from ecom_ops.wp_ops.page_review_loop import PAGE_REVIEW_TOOLS
from ecom_ops.wp_ops.playbooks import PLAYBOOK_TOPICS, PLAYBOOKS

ROOT = Path(__file__).resolve().parents[2]

VALID_WP_TOOLS = frozenset(ALL_FILE_TOOLS) | frozenset(WP_TOOL_NAMES) | frozenset(PAGE_REVIEW_TOOLS)

# Legitimate WordPress/WooCommerce PHP API functions, output fields, and clarify
# category enums that share the wp_/wc_ prefix but are not agent tool names.
NON_TOOL_WP_IDENTIFIERS = frozenset(
    {
        "wc_get_order",
        "wc_tested_up_to",
        "wp_code",
        "wp_enqueue_script",
        "wp_enqueue_style",
        "wp_kses_post",
        "wp_verify_nonce",
        "wp_visual",
    }
)

WP_SKILL_CARDS = (
    ROOT / "skills" / "wordpress-page-builder" / "SKILL.md",
    ROOT / "skills" / "wordpress-plugin-dev" / "SKILL.md",
    ROOT / "skills" / "wordpress-review" / "SKILL.md",
    ROOT / "skills" / "wordpress-theme-dev" / "SKILL.md",
    ROOT / "skills" / "wordpress-ui-iteration" / "SKILL.md",
    ROOT / "skills" / "change-verification" / "SKILL.md",
)
ALL_SKILL_DOCS = (*WP_SKILL_CARDS, ROOT / "skills" / "ecom-ops" / "SKILL.md")
GUARDRAILS_DOC = ROOT / "docs" / "WP_SKILL_GUARDRAILS.md"
WORDPRESS_OPS_DOC = ROOT / "docs" / "WORDPRESS_OPS.md"
PLAYBOOKS_SRC = ROOT / "skills" / "ecom_ops" / "wp_ops" / "playbooks.py"

_BACKTICK_SPAN_RE = re.compile(r"`([^`\n]+)`")
_WP_TOOL_TOKEN_RE = re.compile(r"(?<![./\w])((?:wp|wc)_[a-z][a-z0-9_]*)(?!\s*\.\s*\w)(?![/\w])")
_BARE_WP_PAGE_REVIEW_RE = re.compile(r"\bwp_page_review\b")


def _extract_backticked_wp_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for span in _BACKTICK_SPAN_RE.findall(text):
        for match in _WP_TOOL_TOKEN_RE.findall(span):
            tokens.add(match)
    return tokens


def test_playbooks_reference_only_real_wp_tools() -> None:
    assert NON_TOOL_WP_IDENTIFIERS.isdisjoint(VALID_WP_TOOLS)
    assert "wp_page_review" not in NON_TOOL_WP_IDENTIFIERS

    for topic in PLAYBOOK_TOPICS:
        entry = PLAYBOOKS[topic]
        unknown_tools = [tool for tool in entry["tools"] if tool not in VALID_WP_TOOLS]
        assert unknown_tools == [], f"playbooks[{topic!r}]['tools'] has unknown tools: {unknown_tools}"

    src = PLAYBOOKS_SRC.read_text(encoding="utf-8")
    assert not _BARE_WP_PAGE_REVIEW_RE.search(src), "bare wp_page_review drift found in playbooks.py"
    raw_tokens = set(_WP_TOOL_TOKEN_RE.findall(src)) - NON_TOOL_WP_IDENTIFIERS
    unknown_in_src = sorted(raw_tokens - VALID_WP_TOOLS)
    assert unknown_in_src == [], f"playbooks.py mentions non-existent WP tools: {unknown_in_src}"


def test_wp_skill_cards_and_guardrails_reference_only_real_wp_tools() -> None:
    for path in (*ALL_SKILL_DOCS, GUARDRAILS_DOC):
        text = path.read_text(encoding="utf-8")
        assert not _BARE_WP_PAGE_REVIEW_RE.search(text), f"bare wp_page_review drift in {path.relative_to(ROOT)}"
        found = _extract_backticked_wp_tokens(text) - NON_TOOL_WP_IDENTIFIERS
        unknown = sorted(found - VALID_WP_TOOLS)
        assert unknown == [], f"{path.relative_to(ROOT)} references unknown WP tools: {unknown}"

    wp_ops_doc = WORDPRESS_OPS_DOC.read_text(encoding="utf-8")
    assert not _BARE_WP_PAGE_REVIEW_RE.search(wp_ops_doc), "bare wp_page_review drift in docs/WORDPRESS_OPS.md"


def test_wordpress_review_skill_has_no_leaked_permission_tokens_and_accurate_runtimes() -> None:
    review_skill = (ROOT / "skills" / "wordpress-review" / "SKILL.md").read_text(encoding="utf-8")
    assert "WP_OPS_" not in review_skill
    assert "AZOM_WP_SANDBOX=1" in review_skill
    assert "AZOM_WP_LIVE_PREVIEW=1" in review_skill
    assert "still require isolated runtimes that are not wired in" not in review_skill


def test_wp_skill_cards_link_shared_guardrails_and_fix_ui_numbering() -> None:
    assert GUARDRAILS_DOC.is_file()
    for path in ALL_SKILL_DOCS:
        text = path.read_text(encoding="utf-8")
        assert "docs/WP_SKILL_GUARDRAILS.md" in text, f"{path.relative_to(ROOT)} must reference docs/WP_SKILL_GUARDRAILS.md"

    ui_skill = (ROOT / "skills" / "wordpress-ui-iteration" / "SKILL.md").read_text(encoding="utf-8")
    sec1 = ui_skill.split("## 1. Visual intake", 1)[1].split("## 2. Iterative", 1)[0]
    numbered = re.findall(r"(?m)^(\d+)\.\s+\*\*", sec1)
    assert numbered == ["1", "2", "3"], f"unexpected section 1 numbering in wordpress-ui-iteration/SKILL.md: {numbered}"


def test_wp_docs_and_skills_byte_limits_match_code() -> None:
    result_kib = FILE_LOOP_MAX_RESULT_BYTES // 1024
    total_kib = FILE_LOOP_MAX_TOTAL_BYTES // 1024
    assert (FILE_LOOP_MAX_CALLS, FILE_LOOP_MAX_READS, FILE_LOOP_MAX_SEARCHES) == (16, 10, 5)

    for path in (
        WORDPRESS_OPS_DOC,
        GUARDRAILS_DOC,
        ROOT / "skills" / "ecom-ops" / "SKILL.md",
        ROOT / "skills" / "wordpress-theme-dev" / "SKILL.md",
        ROOT / "skills" / "wordpress-plugin-dev" / "SKILL.md",
        ROOT / "skills" / "wordpress-review" / "SKILL.md",
    ):
        text = path.read_text(encoding="utf-8")
        assert f"{result_kib} KiB" in text, f"{path.relative_to(ROOT)} missing {result_kib} KiB result cap"
        assert f"{total_kib} KiB" in text, f"{path.relative_to(ROOT)} missing {total_kib} KiB total cap"
        assert "16 KiB" not in text, f"{path.relative_to(ROOT)} still has stale 16 KiB cap"
        assert "48 KiB per result" not in text
        assert "/ 48 KiB total" not in text
        assert "48 KiB` total" not in text

    for path in (WORDPRESS_OPS_DOC, GUARDRAILS_DOC, ROOT / "skills" / "ecom-ops" / "SKILL.md"):
        text = path.read_text(encoding="utf-8")
        assert str(_FILE_READ_DEFAULT) in text, f"{path.relative_to(ROOT)} missing default read limit {_FILE_READ_DEFAULT}"
        assert str(_FILE_READ_MAX) in text, f"{path.relative_to(ROOT)} missing max read limit {_FILE_READ_MAX}"
        assert "12288" not in text, f"{path.relative_to(ROOT)} still has stale 12288 read limit"
        assert "4096 bytes and the maximum is 8192" not in text

    wp_ops_text = WORDPRESS_OPS_DOC.read_text(encoding="utf-8")
    assert f"across the {len(PLAYBOOK_TOPICS)} `PLAYBOOK_TOPICS`" in wp_ops_text
    assert "10 `PLAYBOOK_TOPICS`" not in wp_ops_text
