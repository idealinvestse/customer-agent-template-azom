---
name: change-verification
description: >
  Pre-change verification and clarification contract for ambiguous change
  requests, annotated screenshot descriptions, layout/theme requests, and
  multi-path edits. Defines the <clarify>{...}</clarify> JSON protocol,
  clickable button choices, 3-round cap, and strict HITL/RBAC boundaries.
version: "3.0.0"
---

# Pre-change verification and clarification

**Purpose:** Ensure AzomOps double-checks ambiguous change requests, visual/annotated screenshot descriptions, and multi-path edits with focused counter-questions and recommended choices before opening workspaces, proposing patches, or mutating store data.  
**Audience:** Coding agents, prompt maintainers, and the OpenClaw chat agent (`Messenger` and `Telegram`).  
**Read this first:** `AGENTS.md`, `SOUL.md`, `skills/ecom-ops/SKILL.md`, `skills/wordpress-ui-iteration/SKILL.md`, `docs/CURRENT_STATE.md`.

## When clarification triggers

Trigger the pre-change verification step when a user request asks to change something and any of the following applies:

1. **Annotated screenshot or visual references without full spec:** Mentions such as `"ringat in på bilden"`, `"skärmdump"`, `"screenshot"`, `"markerat"`, `"röd ring"`, `"pilen"`, or `"på bilden"` where the target element, selector, or desired outcome can be interpreted in more than one way.
2. **Layout, styling, or theme requests without exact file/diff spec:** Requests like `"gör headern snyggare"`, `"flytta knappen här"`, `"styla om produktsidan"`, or `"förbättra mobilläget"`.
3. **Vague or under-specified targets/values:** Missing explicit numeric ID, page slug, file path, or target value (for example `"ändra den här sidan"`, `"uppdatera produkten"`, `"fixa texten"`).
4. **Multi-path technical changes:** Changes that could reasonably live in multiple layers (for example `theme.json` tokens vs child theme CSS vs WooCommerce template override vs site plugin `azom-site-<site_id>` vs WordPress database page content).

## When to skip clarification

Do **not** ask a clarifying counter-question when:

- The message is a pure read-only query (`visa`, `lista`, `kolla`, `vad är`, `hur många`, `granska`, `vilket tema`, `vilka plugins`, `status på`) without a change verb.
- The message is a slash command (`/...`), short greeting/acknowledgement (`hej`, `tack`, `ok`), or hits an explicit `site_content` refusal (such as installing/deleting plugins, switching themes, or deleting/creating posts).
- The request is already explicit and unambiguous (for example `"sätt order 1001 till completed"`, `"ändra produkt 501 pris till 999"`, `"ändra sidan 2 titel till Kontakt"`, `"ändra sajttitel till Butiken"`).
- The user has already confirmed the clarification scope (`clarification_confirmed=True`).
- An active `wr-*` workspace/preview already anchors the target (`has_active_wp_review=True`) and the follow-up message requests a clear adjustment or visual bug-fix on that proposal (for example `"gör knappen större"`, `"byt till mörkblå"`, or `"fixa ikonöverlappet"`). Continue directly via `skills/wordpress-ui-iteration/SKILL.md` unless the follow-up switches to a new ambiguous target or contradicts itself.
- An annotated screenshot together with the user's message already uniquely identifies both the target element and the requested change.

## `<clarify>{...}</clarify>` contract

When clarifying, emit a single `<clarify>{...}</clarify>` JSON block. The runtime (`skills/ecom_ops/bot/change_clarifier.py`) withholds raw `<clarify>` blocks from streaming drafts, parses and validates the JSON, strips raw tags so they never leak to users, and renders a scannable Swedish card with clickable buttons.

### JSON schema

```json
{
  "understanding": "Kort sammanfattning av vad du har förstått (max 500 tecken).",
  "question": "En fokuserad motfråga som avgör nästa steg (max 500 tecken).",
  "mode": "options",
  "category": "wp_visual",
  "target_summary": "Valfri kort målbeskrivning (max 200 tecken)",
  "plan_steps": ["Valfri punkt 1", "Valfri punkt 2"],
  "options": [
    {
      "id": "opt1",
      "label": "Kort knappetikett (max 44 tecken)",
      "summary": "Vad detta val innebär (max 240 tecken).",
      "action_hint": "Konkret teknisk riktning för nästa steg (max 400 tecken).",
      "recommended": true
    },
    {
      "id": "opt2",
      "label": "Alternativ väg",
      "summary": "Kort beskrivning av alternativet.",
      "action_hint": "Alternativ teknisk riktning.",
      "recommended": false
    }
  ]
}
```

### Field and UX rules

- **`mode`:** `"options"` requires **2–4** options; `"confirm"` allows **0–4** options and presents the confirmation summary before starting work.
- **`category`:** One of `wp_visual`, `wp_code`, `page_content`, `product`, `site_settings`, `order`, `general_change`.
- **Recommended option:** Mark at most **one** option with `"recommended": true`. It is placed **first** and displayed with the `⭐` prefix.
- **Button layout and callbacks (all ≤ 64 UTF-8 bytes, `cl-[0-9a-f]{12}`):**
  - Option picks: `clarify:pick:{cid}:{opt_id}` (label prefixed with `⭐ ` when recommended).
  - Confirm start (in `"confirm"` mode): `"✅ Stämmer — börja arbeta"` (`clarify:go:{cid}`).
  - Custom input: `"✍️ Skriv egen förklaring"` in options mode or `"✍️ Justera / lägg till"` in confirm mode (`clarify:other:{cid}`).
  - Cancel: `"✖️ Avbryt"` (`clarify:cancel:{cid}`).
- **Round cap:** At most **3** clarification rounds (`MAX_CLARIFY_ROUNDS = 3`) per request. Each proposal uses single-use atomic consumption.
- **Messenger 3-button cap:** Messenger prioritizes `clarify:go:` and `clarify:pick:` ahead of `clarify:other:` and `clarify:cancel:`, and users can always reply with option numbers (`1`–`4`) or free text.
- **Preserve target anchors:** When `<clarify>` is emitted for a WordPress/UI change, preserve any known `site_id`, `plan_id` (`wr-*`), `path`, and `focus_selector` inside `target_summary` and `action_hint` so subsequent turns retain context.

## Hard boundaries

**Do:**

- Run read-only inspection tools (`wp_theme_overview`, `wp_template_hierarchy`, `wp_page_review`, `list_pages`, `show_product`, etc.) before or while clarifying so options reflect actual site state.
- Keep counter-questions short, concrete, and focused on resolving the ambiguity.

**Do not:**

- Write files, open/patch `wr-*` workspaces for unconfirmed changes, mutate WooCommerce/WordPress data, or send customer mail during clarification.
- Treat `clarify:pick` or `clarify:go` (`"✅ Stämmer — börja arbeta"`) as write authorization. Clarification only locks scope and intent so the agent can inspect and prepare a proposal.
- Bypass or replace explicit HITL approval gates or RBAC:
  - Order/product/content/plugin writes still require `proposal:yes:pr-...`, `action:yes`, `order:set:...`, or `product:desc:...` with the required actor role.
  - Single-file staging still requires `wpfile:stage:fe-...`; multi-file workspace staging still requires checks plus `wpreview:stage:wr-...` (Oscar `WP_OPS_ADMIN` + `CODE_EDIT`).
  - Customer email still requires `/cases approve`, **Godkänn & skicka**, dashboard approve, or authorized CLI `cases reply`.
