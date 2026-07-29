# Support classify fixtures (SB4 / FU7)

Anonymized subject+body samples for regression of:

1. **Keyword** category (`classify_message`)
2. **Suggest-approve eligibility** (`is_suggest_approve_eligible`) given category + confidence + order_id

No raw customer PII. Thresholds must only change with notes here or in `docs/solutions/`.

Includes SV Path B pack plus azom.no (bokmål) scenarios from 2026-07-29 vNext (angrerett, sporing, CarPlay compat, Datatilsynet).

Schema per JSON file:

```json
{
  "id": "unique-slug",
  "text": "Subject / body text",
  "expected_category": "order_status|shipping|return|billing|abuse|product|technical|other",
  "order_id_in_text": "1001|null",
  "suggest_with_llm_confidence": 0.91,
  "expect_suggest_approve": false
}
```

`suggest_with_llm_confidence` is the hypothetical confidence if hybrid used LLM;
eligibility still requires allowlisted category + order_id (when required) + never-list.
