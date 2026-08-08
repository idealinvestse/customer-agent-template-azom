"""Format FAQ hits for LLM prompts and internal audit footers."""

from __future__ import annotations

from ecom_ops.faq.config import load_faq_config
from ecom_ops.faq.models import FaqHit


def format_faq_context_block(hits: list[FaqHit]) -> str:
    """Prompt block for draft_support_with_llm (empty string if no hits)."""
    if not hits:
        return ""
    cfg = load_faq_config()
    lines: list[str] = []
    for hit in hits:
        body = hit.snippet or hit.article.body
        body = " ".join(body.split())
        if len(body) > cfg.max_chars_per_hit:
            body = body[: cfg.max_chars_per_hit - 1].rstrip() + "…"
        lines.append(f"- [{hit.article.id}] {hit.article.title}: {body}")
    return "\n".join(lines)


def format_faq_citation_footer(hits: list[FaqHit]) -> str:
    """Internal audit footer — do not append to customer mail by default."""
    if not hits:
        return ""
    ids = ", ".join(h.article.id for h in hits)
    return f"[FAQ refs: {ids}]"
