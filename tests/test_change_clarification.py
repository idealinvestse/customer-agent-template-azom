"""Tests for pre-change verification & clarification across chat, Telegram, and Messenger."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from ecom_ops.bot import __main__ as bot_main
from ecom_ops.bot import chat_agent, handlers
from ecom_ops.bot.change_clarifier import (
    MAX_CLARIFY_ROUNDS,
    ClarificationOption,
    ClarificationProposal,
    build_fallback_clarification,
    detect_change_ambiguity,
    has_clarify_tag,
    parse_clarify_block,
    render_clarification_message,
    strip_clarify_tags,
)
from ecom_ops.bot.chat_agent import (
    FALLBACK_BUDGET,
    FALLBACK_NO_KEY,
    ChatResult,
    _SafeTextStream,
    run_chat,
)
from ecom_ops.bot.handlers import BotHandler
from ecom_ops.bot.job_store import TelegramJobStore
from ecom_ops.bot.messenger_adapter import actions_to_messenger_buttons
from ecom_ops.bot.reply import clarification_actions
from ecom_ops.bot.store import ConversationStore
from ecom_ops.wp_ops.agent_tools import context_for_storage


@pytest.fixture()
def env_setup(monkeypatch, tmp_path):
    monkeypatch.setenv("AZOM_USE_MOCK", "1")
    monkeypatch.setenv("AZOM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "100,200,300")
    monkeypatch.setenv("TELEGRAM_ACTOR_MAP", "100:oscar,200:agent,300:jonatan")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(
        "ecom_ops.bot.chat_agent.Telemetry.within_budget", lambda self, cap: True
    )
    return tmp_path


def _sample_clarify_json(
    *,
    mode: str = "options",
    round_num: int = 1,
    question: str = "Vilken del av sidan är det du vill ändra?",
    options: list[dict[str, Any]] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "mode": mode,
        "category": "wp_visual",
        "target_summary": "Startsidan / knapp",
        "understanding": "Du vill justera elementet som är markerat på bilden.",
        "question": question,
        "plan_steps": ["Inspektera nuvarande stil", "Ta fram förslag"],
        "round_num": round_num,
    }
    if mode == "options":
        payload["options"] = options or [
            {
                "id": "css_btn",
                "label": "Justera knappens CSS",
                "summary": "Ändra färg/storlek via temats CSS.",
                "action_hint": "Föreslå säker CSS-justering",
                "recommended": True,
            },
            {
                "id": "page_block",
                "label": "Ändra text/block på sidan",
                "summary": "Redigera sidans innehåll eller rubrik.",
                "action_hint": "Kolla sidans block först",
            },
            {
                "id": "inspect",
                "label": "Granska sidan först",
                "summary": "Visa nuvarande upplägg innan ändring.",
                "action_hint": "Read-only sidkontroll",
            },
        ]
    else:
        payload["options"] = options or []
    return f"<clarify>{json.dumps(payload, ensure_ascii=False)}</clarify>"


# ---------------------------------------------------------------------------
# 1. Ambiguity detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected_cat"),
    [
        ("Kan du fixa det jag har ringat in på bilden?", "wp_visual"),
        ("Ändra den röda ringen på skärmdumpen så den blir större", "wp_visual"),
        ("Ta bort det som är markerat på printen", "wp_visual"),
        ("Gör headern snyggare och centrera loggan", "wp_visual"),
        ("Fixa CSS i temat så den blir modernare", "wp_code"),
        ("Justera functions.php så det funkar bättre", "wp_code"),
        ("Uppdatera texten på sidan", "page_content"),
        ("Ändra priset på produkten", "product"),
        ("Ändra status på min order", "order"),
    ],
)
def test_detect_change_ambiguity_flags_vague_and_screenshot_requests(text, expected_cat):
    res = detect_change_ambiguity(text)
    assert res.is_change_request is True
    assert res.is_ambiguous is True
    assert res.category == expected_cat
    assert res.reasons
    if any(w in text.lower() for w in ("bilden", "skärmdumpen", "printen")):
        assert res.mentions_screenshot_or_annotation is True


@pytest.mark.parametrize(
    "text",
    [
        "sätt order 1001 till completed",
        "ändra produkt 501 pris till 999",
        "ändra sidan 2 titel till Kontakt",
        "ändra inlägg 1 titel till Ny rubrik",
        "ändra sajttitel till Butiken",
        "aktivera plugin akismet",
    ],
)
def test_detect_change_ambiguity_allows_explicit_clear_writes(text):
    res = detect_change_ambiguity(text)
    assert res.is_change_request is True
    assert res.is_ambiguous is False
    assert res.reasons == ()


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Hej",
        "tack",
        "ok",
        "/cases list",
        "/wp use site-se",
        "Visa order 1001",
        "Hur många öppna ärenden har vi?",
        "Lista produkter",
        "Granska startsidan",
        "installera plugin woocommerce",
        "byt tema till storefront",
    ],
)
def test_detect_change_ambiguity_skips_reads_acks_commands_and_refusals(text):
    res = detect_change_ambiguity(text)
    assert res.is_change_request is False
    assert res.is_ambiguous is False


def test_detect_change_ambiguity_skips_when_confirmed_or_pending():
    assert detect_change_ambiguity(
        "Gör headern snyggare", clarification_confirmed=True
    ).is_ambiguous is False
    assert detect_change_ambiguity(
        "Gör headern snyggare", has_pending_action=True
    ).is_ambiguous is False


# ---------------------------------------------------------------------------
# 2. Parser, scrubber, renderer, and 64-byte callback limits
# ---------------------------------------------------------------------------


def test_parse_clarify_block_validates_fences_recommended_and_scrubs_secrets():
    raw = (
        "Jag vill dubbelkolla.\n"
        "<clarify>```json\n"
        + json.dumps(
            {
                "mode": "options",
                "category": "wp_visual",
                "understanding": "Token Bearer sk-secret12345678 bör ej synas <tool_call>{}</tool_call>",
                "question": "Vad vill du ändra?",
                "options": [
                    {"id": "opt_a", "label": "Alternativ A", "summary": "Första", "recommended": False},
                    {"id": "opt_b", "label": "Alternativ B", "summary": "Andra", "recommended": True},
                    {"id": "opt_c", "label": "Alternativ C", "summary": "Tredje", "recommended": True},
                ],
            }
        )
        + "\n```</clarify>"
    )
    prop = parse_clarify_block(raw, original_request="Fixa bilden", round_num=1)
    assert prop is not None
    assert "sk-secret" not in prop.understanding
    assert "[REDACTED]" in prop.understanding
    assert "<tool_call>" not in prop.understanding
    # At most one recommended=True, sorted first
    assert [o.recommended for o in prop.options] == [True, False, False]
    assert prop.options[0].id == "opt_b"

    # Invalid options count (<2) in options mode returns None
    bad = '<clarify>{"mode":"options","understanding":"u","question":"q","options":[{"id":"1","label":"Enda"}]}</clarify>'
    assert parse_clarify_block(bad) is None

    # Confirm mode allows 0 options
    conf = '<clarify>{"mode":"confirm","understanding":"u","question":"Stämmer detta?","options":[]}</clarify>'
    conf_prop = parse_clarify_block(conf)
    assert conf_prop is not None
    assert conf_prop.mode == "confirm"
    assert conf_prop.options == ()


def test_render_clarification_and_64_byte_callback_limit():
    cid = "cl-abcdef012345"
    prop = ClarificationProposal(
        understanding="Du vill justera knappen.",
        question="Vilket spår väljer du?",
        options=(
            ClarificationOption(
                id="very_long_option_identifier_exceeding",
                label="Långt alternativnamn som testas",
                summary="Ändra CSS",
                recommended=False,
            ),
            ClarificationOption(
                id="rec_id",
                label="Rekommenderat spår",
                summary="Öppna arbetsyta",
                recommended=True,
            ),
        ),
        plan_steps=("Steg 1", "Steg 2"),
        round_num=2,
    )
    rendered = render_clarification_message(prop, intro="Kort intro.")
    assert "🔍 Dubbelkoll innan jag börjar arbeta (steg 2/3)" in rendered
    assert "⭐ Rekommenderat spår (rekommenderat)" in rendered
    assert "Ingen ändring utförs förrän vi är överens" in rendered

    actions = clarification_actions(cid, prop)
    for row in actions.rows:
        for btn in row:
            assert btn.payload is not None
            assert len(btn.payload.encode("utf-8")) <= 64
            assert re.match(r"^clarify:(?:pick|go|other|cancel):cl-[0-9a-f]{12}", btn.payload)


# ---------------------------------------------------------------------------
# 3. _SafeTextStream withholding <tool_call> and <clarify> across chunks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "full_text",
    [
        'Jag kollar först.\n<clarify>{"mode":"options","understanding":"u","question":"q","options":[{"id":"1","label":"A"},{"id":"2","label":"B"}]}</clarify>',
        'Intro text <CLARIFY>{"mode":"confirm","understanding":"u","question":"q"}</CLARIFY>',
        'Vänta <tool_call>{"name":"lookup_order","arguments":{"order_id":"1001"}}</tool_call>',
    ],
)
def test_safe_text_stream_withholds_markers_across_arbitrary_chunks(full_text):
    emitted: list[str] = []
    stream = _SafeTextStream(emitted.append, lambda: emitted.clear())
    # Feed 1-char chunks to stress every possible split boundary
    for ch in full_text:
        stream.push(ch)
    stream.finish(full_text)
    joined = "".join(emitted)
    assert "<clarify" not in joined.lower()
    assert "<tool_call" not in joined.lower()
    assert "{" not in joined
    assert has_clarify_tag(full_text) or "<tool_call>" in full_text.lower()
    assert strip_clarify_tags("<clarify>{bad}</clarify> Rent svar") == "Rent svar"


def test_safe_text_stream_flushes_harmless_angle_brackets():
    emitted: list[str] = []
    stream = _SafeTextStream(emitted.append, lambda: emitted.clear())
    msg = "Priset < 500 kr och <clara> är ok <"
    for idx in range(0, len(msg), 2):
        stream.push(msg[idx : idx + 2])
    stream.finish(msg)
    assert "".join(emitted) == msg


# ---------------------------------------------------------------------------
# 4. TelegramJobStore id_prefix & non-action callback routing
# ---------------------------------------------------------------------------


def test_job_store_id_prefix_and_clarify_non_action_routing(env_setup):
    jstore = TelegramJobStore(env_setup / "jobs.sqlite3")
    p_default = jstore.create_proposal(100, kind="order_status", payload={"order_id": "1001"})
    p_cl = jstore.create_proposal(
        100, kind="change_clarify", payload={"round": 1}, id_prefix="cl"
    )
    p_bad = jstore.create_proposal(
        100, kind="order_status", payload={}, id_prefix="INVALID_PREFIX"
    )
    assert re.fullmatch(r"pr-[0-9a-f]{12}", p_default["proposal_id"])
    assert re.fullmatch(r"cl-[0-9a-f]{12}", p_cl["proposal_id"])
    assert re.fullmatch(r"pr-[0-9a-f]{12}", p_bad["proposal_id"])

    # Clarify callbacks must stay kind="callback" (never "action")
    cid = p_cl["proposal_id"]
    for idx, cb in enumerate(
        (
            f"clarify:pick:{cid}:css_btn",
            f"clarify:go:{cid}",
            f"clarify:other:{cid}",
            f"clarify:cancel:{cid}",
        ),
        start=10,
    ):
        res = jstore.accept_update(
            {
                "update_id": idx,
                "callback_query": {
                    "id": f"cb-{idx}",
                    "data": cb,
                    "message": {"message_id": idx, "chat": {"id": 100}},
                },
            }
        )
        assert res["disposition"] == "queued"
        job = jstore.get_job(res["job_id"])
        assert job is not None
        assert job["kind"] == "callback"


def test_main_process_job_forwards_streaming_kwargs_to_callback(env_setup, monkeypatch):
    jstore = TelegramJobStore(env_setup / "jobs.sqlite3")
    cstore = ConversationStore(env_setup / "state.json")
    bot = BotHandler(store=cstore, proposal_store=jstore)

    seen_kwargs: dict[str, Any] = {}

    def spy_handle_callback(chat_id, data, **kwargs):
        seen_kwargs.update(kwargs)
        return handlers.BotReply(text="Callback ok")

    monkeypatch.setattr(bot, "handle_callback", spy_handle_callback)
    monkeypatch.setattr(
        bot_main,
        "_api",
        lambda token, method, **params: {"ok": True, "result": {"message_id": 42}},
    )

    accepted = jstore.accept_update(
        {
            "update_id": 99,
            "callback_query": {
                "id": "cb-99",
                "data": "clarify:go:cl-000000000001",
                "message": {"message_id": 9, "chat": {"id": 100}},
            },
        }
    )
    job = jstore.claim_next()
    assert job is not None and job["job_id"] == accepted["job_id"]
    bot_main._process_job("tok", bot, jstore, job)

    for key in (
        "on_progress",
        "on_text_delta",
        "on_text_reset",
        "on_stream_clear",
        "on_phase_change",
        "on_checkpoint",
        "task_id",
    ):
        assert callable(seen_kwargs[key]) if key != "task_id" else seen_kwargs[key] == job["task_id"]


# ---------------------------------------------------------------------------
# 5. BotHandler multi-round clarification flows (pick/go/other/cancel/double-click/text)
# ---------------------------------------------------------------------------


def test_bot_handler_multi_round_pick_double_click_and_go(env_setup, monkeypatch):
    cstore = ConversationStore(env_setup / "state.json")
    jstore = TelegramJobStore(env_setup / "jobs.sqlite3")
    bot = BotHandler(store=cstore, proposal_store=jstore)

    calls: list[dict[str, Any]] = []

    def fake_chat_completion(messages, **kwargs):
        calls.append({"messages": messages, **kwargs})
        n = len(calls)
        if n == 1:
            return _sample_clarify_json(mode="options", round_num=1), 0.0
        if n == 2:
            return (
                _sample_clarify_json(
                    mode="confirm",
                    round_num=2,
                    question="Jag ändrar knappens CSS till större grön knapp. Stämmer det?",
                ),
                0.0,
            )
        return "Bekräftat — här är planen efter din bekräftelse.", 0.0

    monkeypatch.setattr(chat_agent, "chat_completion", fake_chat_completion)

    # Round 1: ambiguous annotated screenshot request
    r1 = bot.handle(100, "Kan du fixa det jag har ringat in på bilden?")
    assert r1.actions is not None
    assert "<clarify>" not in r1.text.lower()
    assert "🔍 Dubbelkoll innan jag börjar arbeta" in r1.text
    pick_cb = r1.actions.rows[0][0].payload
    assert pick_cb is not None and pick_cb.startswith("clarify:pick:cl-")

    state1 = cstore.get(100)
    assert state1 and state1["flow"] == "change_clarify"
    assert state1["step"] == "await_choice"

    # Round 2: click first option -> model returns mode="confirm"
    r2 = bot.handle_callback(100, pick_cb)
    assert "Justera knappens CSS" in str(calls[1]["messages"])
    assert r2.actions is not None
    assert "✅ Bekräfta innan jag börjar arbeta (steg 2/3)" in r2.text
    go_cb = r2.actions.rows[0][0].payload
    assert go_cb is not None and go_cb.startswith("clarify:go:cl-")

    # Double-click on old round-1 pick MUST be rejected safely without calling model again
    dup = bot.handle_callback(100, pick_cb)
    assert "redan hanterat eller utgånget" in dup.text.lower()
    assert len(calls) == 2

    state2 = cstore.get(100)
    assert state2 and state2["flow"] == "change_clarify"
    assert state2["step"] == "await_confirm"

    # Confirm via clarify:go -> passes clarification_confirmed=True
    r3 = bot.handle_callback(100, go_cb)
    assert "Bekräftat" in r3.text
    assert r3.actions is None
    assert len(calls) == 3
    assert "bekräftat inriktningen" in str(calls[2]["messages"]).lower()
    state3 = cstore.get(100)
    assert state3 is None or state3.get("flow") is None

    # Double-click on clarify:go is also rejected
    dup_go = bot.handle_callback(100, go_cb)
    assert "redan hanterat eller utgånget" in dup_go.text.lower()
    assert len(calls) == 3


def test_bot_handler_clarify_other_and_cancel_and_max_rounds(env_setup, monkeypatch):
    assert MAX_CLARIFY_ROUNDS == 3
    cstore = ConversationStore(env_setup / "state.json")
    jstore = TelegramJobStore(env_setup / "jobs.sqlite3")
    bot = BotHandler(store=cstore, proposal_store=jstore)

    def fake_chat_completion(messages, **kwargs):
        # Model keeps trying to emit <clarify> even past MAX_CLARIFY_ROUNDS
        return _sample_clarify_json(mode="options") + "\nFortsätter nu.", 0.0

    monkeypatch.setattr(chat_agent, "chat_completion", fake_chat_completion)

    r1 = bot.handle(100, "Gör knappen snyggare")
    assert r1.actions is not None
    other_cb = r1.actions.rows[-1][0].payload
    cancel_cb = r1.actions.rows[-1][1].payload
    assert other_cb and other_cb.startswith("clarify:other:cl-")
    assert cancel_cb and cancel_cb.startswith("clarify:cancel:cl-")

    # Test cancel
    rc = bot.handle_callback(100, cancel_cb)
    assert "Avbrutet" in rc.text
    assert (cstore.get(100) or {}).get("flow") is None
    # Clicking other after cancel fails closed
    assert "redan hanterat" in bot.handle_callback(100, other_cb).text.lower()

    # Start again and use clarify:other
    r2 = bot.handle(100, "Gör knappen snyggare")
    other_cb2 = r2.actions.rows[-1][0].payload
    ro = bot.handle_callback(100, other_cb2)
    assert "egna ord" in ro.text.lower()
    assert (cstore.get(100) or {}).get("step") == "await_other_text"

    # Provide explanation on round 2 -> round 2 clarification
    r3 = bot.handle(100, "Den blå köpknappen på produktsidan")
    assert r3.actions is not None
    pick2 = r3.actions.rows[0][0].payload
    # Pick on round 2 -> round 3 clarification
    r4 = bot.handle_callback(100, pick2)
    assert r4.actions is not None
    pick3 = r4.actions.rows[0][0].payload
    # Pick on round 3 -> next_round=4 > MAX_CLARIFY_ROUNDS -> confirmed, raw <clarify> stripped!
    r5 = bot.handle_callback(100, pick3)
    assert "<clarify>" not in r5.text.lower()
    assert "Fortsätter nu." in r5.text
    assert r5.actions is None


def test_continue_flow_text_pick_confirm_cancel_and_lookup_breakout(env_setup, monkeypatch):
    cstore = ConversationStore(env_setup / "state.json")
    jstore = TelegramJobStore(env_setup / "jobs.sqlite3")
    bot = BotHandler(store=cstore, proposal_store=jstore)

    seen_run_chat: list[dict[str, Any]] = []

    def fake_run_chat(message, **kwargs):
        seen_run_chat.append({"message": message, **kwargs})
        if kwargs.get("clarification_confirmed"):
            return ChatResult(text="Kör vidare efter ja.", messages=[{"role": "user", "content": message}, {"role": "assistant", "content": "Kör vidare efter ja."}])
        if kwargs.get("clarification_context"):
            return ChatResult(
                text="Bekräfta nu?",
                messages=[{"role": "user", "content": message}, {"role": "assistant", "content": "Bekräfta nu?"}],
                clarification=ClarificationProposal(
                    understanding="Valt alternativ 1",
                    question="Stämmer det?",
                    mode="confirm",
                    round_num=2,
                ),
            )
        return ChatResult(
            text="Dubbelkoll?",
            messages=[{"role": "user", "content": message}, {"role": "assistant", "content": "Dubbelkoll?"}],
            clarification=ClarificationProposal(
                understanding="Oklart",
                question="Vilket val?",
                options=(
                    ClarificationOption(id="a", label="Val A", summary="A", recommended=True),
                    ClarificationOption(id="b", label="Val B", summary="B"),
                ),
                mode="options",
            ),
        )

    monkeypatch.setattr(handlers, "run_chat", fake_run_chat)

    # 1) Text pick "1" in await_choice, then "stämmer" in await_confirm
    r1 = bot.handle(100, "Gör sidan snyggare")
    old_btn = r1.actions.rows[0][0].payload
    r2 = bot.handle(100, "1")
    assert (cstore.get(100) or {}).get("step") == "await_confirm"
    # Old round-1 button was consumed by text pick "1"
    assert "redan hanterat" in bot.handle_callback(100, old_btn).text.lower()
    r3 = bot.handle(100, "stämmer")
    assert "Kör vidare efter ja." in r3.text
    assert seen_run_chat[-1]["clarification_confirmed"] is True

    # 2) Unrelated lookup breakout during await_choice
    bot.handle(100, "Gör sidan snyggare")
    monkeypatch.setattr(
        handlers,
        "tool_lookup_order",
        lambda oid, domain=None: f"Order #{oid}\nStatus: processing",
    )
    breakout = bot.handle(100, "order 1001")
    assert "1001" in breakout.text
    assert (cstore.get(100) or {}).get("flow") is None

    # 3) Cancel word during await_choice
    bot.handle(100, "Gör sidan snyggare")
    cancelled = bot.handle(100, "avbryt")
    assert "Avbrutet" in cancelled.text
    assert (cstore.get(100) or {}).get("flow") is None


def test_session_fallback_without_proposal_store_enforces_single_use(env_setup, monkeypatch):
    cstore = ConversationStore(env_setup / "state.json")
    bot = BotHandler(store=cstore, channel="messenger", proposal_store=None)

    monkeypatch.setattr(
        chat_agent,
        "chat_completion",
        lambda messages, **kwargs: (
            (_sample_clarify_json(mode="options"), 0.0)
            if len(messages) < 4
            else ("Klart efter förtydligande.", 0.0)
        ),
    )

    r1 = bot.handle(100, "Ändra det som är ringat in på bilden")
    assert r1.actions is not None
    pick_cb = r1.actions.rows[0][0].payload
    assert pick_cb is not None
    r2 = bot.handle_callback(100, pick_cb)
    assert "Klart" in r2.text or r2.actions is not None
    # Replay same button without proposal_store must still be rejected
    r2_dup = bot.handle_callback(100, pick_cb)
    assert "redan hanterat eller utgånget" in r2_dup.text.lower()


# ---------------------------------------------------------------------------
# 6. File-tool-loop clarification
# ---------------------------------------------------------------------------


def test_file_tool_loop_allows_read_then_clarify_without_writing(env_setup, monkeypatch):
    cstore = ConversationStore(env_setup / "state.json")
    wp_ctx = context_for_storage(
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:100",
        site_id="site-se",
        operation_ids=[],
    )
    cstore.set(
        100,
        {
            "flow": None,
            "step": None,
            "slots": {},
            "session": {"wp_ops_context": wp_ctx},
            "messages": [],
            "tool_digest": "",
        },
    )

    calls = {"n": 0}

    def fake_chat_completion(messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return (
                '<tool_call>{"name":"wp_theme_overview","arguments":{"site_id":"site-se"}}</tool_call>',
                0.0,
            )
        return _sample_clarify_json(mode="options"), 0.0

    monkeypatch.setattr(chat_agent, "chat_completion", fake_chat_completion)

    res = run_chat(
        "Gör headern i style.css snyggare",
        session={"wp_ops_context": wp_ctx},
        actor="oscar",
        channel="telegram",
        conversation_id="telegram:100",
    )
    assert calls["n"] == 2
    assert res.clarification is not None
    assert res.file_stage_plan_id is None
    assert res.review_stage_plan_id is None
    assert "<clarify>" not in res.text.lower()
    assert "🔍 Dubbelkoll innan jag börjar arbeta" in res.text


# ---------------------------------------------------------------------------
# 7. Offline fallback (no LLM / budget exhausted)
# ---------------------------------------------------------------------------


def test_offline_no_key_and_budget_clarify_ambiguous_but_keep_greetings_unchanged(
    env_setup, monkeypatch
):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    class NoRoutes:
        def any_configured(self):
            return False

    monkeypatch.setattr("ecom_ops.bot.chat_agent.get_model_router", lambda: NoRoutes())

    # Greetings stay untouched
    assert run_chat("Hej", history=[], session={}).text == FALLBACK_NO_KEY
    assert run_chat("tack", history=[], session={}).text == FALLBACK_NO_KEY
    assert run_chat("ok", history=[], session={}).text == FALLBACK_NO_KEY

    # Ambiguous change gets offline clarification card + buttons
    cstore = ConversationStore(env_setup / "state.json")
    jstore = TelegramJobStore(env_setup / "jobs.sqlite3")
    bot = BotHandler(store=cstore, proposal_store=jstore)
    r1 = bot.handle(100, "Uppdatera texten på sidan")
    assert r1.actions is not None
    assert FALLBACK_NO_KEY not in r1.text
    assert "🔍 Dubbelkoll innan jag börjar arbeta" in r1.text

    # Picking the "list_pages" option executes deterministic read-only list_pages!
    list_cb = next(
        btn.payload
        for row in r1.actions.rows
        for btn in row
        if btn.payload and btn.payload.endswith(":list_pages")
    )
    r_read = bot.handle_callback(100, list_cb)
    assert FALLBACK_NO_KEY not in r_read.text
    assert "Sidor" in r_read.text or "sid" in r_read.text.lower()

    # Budget exhausted also clarifies ambiguous changes but preserves FALLBACK_BUDGET for "Hej"
    class OpenRouterOnly:
        def any_configured(self):
            return True

        def has_route_outside_openrouter_budget(self):
            return False

    class ExhaustedTel:
        def within_budget(self, cap):
            return False

        def sum_cost_usd(self):
            return 999.0

        def record(self, **kwargs):
            pass

    monkeypatch.setattr("ecom_ops.bot.chat_agent.get_model_router", lambda: OpenRouterOnly())
    assert (
        run_chat("Hej", history=[], session={}, telemetry=ExhaustedTel()).text
        == FALLBACK_BUDGET
    )
    amb_budget = run_chat(
        "Ändra layouten på bilden", history=[], session={}, telemetry=ExhaustedTel()
    )
    assert amb_budget.clarification is not None
    assert "🔍 Dubbelkoll innan jag börjar arbeta" in amb_budget.text


# ---------------------------------------------------------------------------
# 8. Messenger 3-button priority
# ---------------------------------------------------------------------------


def test_messenger_3_button_priority_for_clarification():
    cid = "cl-1234567890ab"
    prop = build_fallback_clarification(
        "Fixa det jag ringat in på bilden",
        detect_change_ambiguity("Fixa det jag ringat in på bilden"),
    )
    actions = clarification_actions(cid, prop)
    buttons = actions_to_messenger_buttons(actions)
    assert len(buttons) == 3
    assert all(b["payload"].startswith("clarify:pick:") for b in buttons)

    confirm_prop = ClarificationProposal(
        understanding="Uppdatera knapp",
        question="Stämmer?",
        mode="confirm",
    )
    confirm_btns = actions_to_messenger_buttons(clarification_actions(cid, confirm_prop))
    assert [b["payload"] for b in confirm_btns] == [
        f"clarify:go:{cid}",
        f"clarify:other:{cid}",
        f"clarify:cancel:{cid}",
    ]


# ---------------------------------------------------------------------------
# 9. HITL & RBAC non-bypass invariants
# ---------------------------------------------------------------------------


def test_clarify_never_bypasses_hitl_or_rbac(env_setup, monkeypatch):
    cstore = ConversationStore(env_setup / "state.json")
    jstore = TelegramJobStore(env_setup / "jobs.sqlite3")
    bot = BotHandler(store=cstore, proposal_store=jstore)

    executed: list[str] = []
    monkeypatch.setattr(
        handlers,
        "execute_order_status",
        lambda order_id, status, actor, domain=None: (executed.append(order_id) or True, "ok"),
    )
    monkeypatch.setattr(
        chat_agent,
        "chat_completion",
        lambda messages, **kwargs: (_sample_clarify_json(mode="confirm"), 0.0),
    )

    # Start clarification as Jonatan (viewer, chat 300)
    r1 = bot.handle(300, "Ändra orderstatusen")
    assert r1.actions is not None
    go_cb = r1.actions.rows[0][0].payload
    assert go_cb and go_cb.startswith("clarify:go:cl-")

    # Confirming clarification does NOT execute any write
    r2 = bot.handle_callback(300, go_cb)
    assert executed == []
    assert "clarify:" not in str(r2.actions or "")

    # Even if user subsequently sends an explicit write, Jonatan STILL gets RBAC-denied without a button!
    r3 = bot.handle(300, "sätt order 1001 till completed")
    assert executed == []
    assert r3.actions is None
    assert "operator" in r3.text.lower() or "oscar" in r3.text.lower()

    # For operator (chat 200), explicit write creates proposal:yes:pr-... (NOT auto-executed!)
    r_op = bot.handle(200, "sätt order 1001 till completed")
    assert executed == []
    assert r_op.actions is not None
    assert r_op.actions.rows[0][0].payload.startswith("proposal:yes:pr-")


def test_clarification_preserves_inbound_screenshot_across_rounds(env_setup, monkeypatch, tmp_path):
    assess = detect_change_ambiguity("ändra den inringade knappen", has_images=True)
    assert assess.mentions_screenshot_or_annotation is True
    assert assess.is_change_request is True
    assert assess.is_ambiguous is True

    img_path = tmp_path / "annotated.png"
    img_path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
        b"\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01\xf6\x178U"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    resolved_img = str(img_path.resolve())

    cstore = ConversationStore(env_setup / "state.json")
    jstore = TelegramJobStore(env_setup / "jobs.sqlite3")
    bot = BotHandler(store=cstore, proposal_store=jstore)

    captured_calls: list[list[dict[str, Any]]] = []

    def fake_completion(messages, **kwargs):
        captured_calls.append(list(messages))
        if len(captured_calls) == 1:
            return _sample_clarify_json(mode="options"), 0.0
        return "Här är nästa steg för den inringade knappen.", 0.0

    monkeypatch.setattr(chat_agent, "chat_completion", fake_completion)
    monkeypatch.setattr(
        chat_agent,
        "_use_read_tool_loop",
        lambda *args, **kwargs: False,
    )

    r1 = bot.handle(100, "Ändra den inringade knappen", image_paths=[str(img_path)])
    assert r1.actions is not None
    pick_cb = r1.actions.rows[0][0].payload
    assert pick_cb and pick_cb.startswith("clarify:pick:cl-")
    assert any(m.get("images") == [resolved_img] for m in captured_calls[0])

    r2 = bot.handle_callback(100, pick_cb)
    assert len(captured_calls) >= 2
    assert any(m.get("images") == [resolved_img] for m in captured_calls[1])
    assert "inringade" in r2.text.lower() or r2.actions is None


def _make_tiny_png(path) -> str:
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
        b"\x00\x00\x00\x0cIDATx\x9cc```\x00\x00\x00\x04\x00\x01\xf6\x178U"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    return str(path.resolve())


def test_history_append_turn_atomic_past_40_messages_limit(env_setup, monkeypatch):
    cstore = ConversationStore(env_setup / "state.json")
    jstore = TelegramJobStore(env_setup / "jobs.sqlite3")
    bot = BotHandler(store=cstore, proposal_store=jstore)

    initial_messages = [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"old-turn-{i}",
        }
        for i in range(40)
    ]
    cstore.set(
        100,
        {
            "flow": None,
            "step": None,
            "slots": {},
            "session": {},
            "messages": initial_messages,
            "tool_digest": "",
        },
    )

    monkeypatch.setattr(chat_agent, "_use_read_tool_loop", lambda *a, **k: False)
    monkeypatch.setattr(
        chat_agent,
        "chat_completion",
        lambda messages, **kwargs: ("Här är svaret efter tur 20.", 0.0),
    )

    reply = bot.handle(100, "ny fråga efter 40 meddelanden")
    assert "tur 20" in reply.text
    after = cstore.get(100)["messages"]
    assert len(after) == 40
    assert after[-2] == {"role": "user", "content": "ny fråga efter 40 meddelanden"}
    assert after[-1] == {"role": "assistant", "content": "Här är svaret efter tur 20."}
    assert after[0]["content"] == "old-turn-2"


def test_clarify_pick_stores_concise_history_label(env_setup, monkeypatch):
    cstore = ConversationStore(env_setup / "state.json")
    jstore = TelegramJobStore(env_setup / "jobs.sqlite3")
    bot = BotHandler(store=cstore, proposal_store=jstore)

    calls: list[int] = []

    def fake_completion(messages, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return _sample_clarify_json(mode="options"), 0.0
        return "Klart med produktsidans knapp.", 0.0

    monkeypatch.setattr(chat_agent, "chat_completion", fake_completion)
    monkeypatch.setattr(chat_agent, "_use_read_tool_loop", lambda *a, **k: False)

    r1 = bot.handle(100, "Ändra knappen där")
    pick_cb = r1.actions.rows[0][0].payload
    r2 = bot.handle_callback(100, pick_cb)
    assert "Klart" in r2.text

    msgs = cstore.get(100)["messages"]
    assert any(
        m.get("role") == "user"
        and m.get("content", "").startswith("[Valt förtydligande:")
        for m in msgs
    )
    assert not any("[Förtydligat underlag" in str(m.get("content") or "") for m in msgs)


def test_active_wp_review_persistence_followup_images_and_clearing(
    env_setup, monkeypatch, tmp_path
):
    from types import SimpleNamespace

    from ecom_ops.wp_ops.review_plan import ReviewPlans

    inbound_png = _make_tiny_png(tmp_path / "inbound_annotated.png")
    focus_png = _make_tiny_png(tmp_path / "focus-desktop.png")
    live_png = _make_tiny_png(tmp_path / "live-desktop.png")
    preview_png = _make_tiny_png(tmp_path / "preview-desktop.png")

    cstore = ConversationStore(env_setup / "state.json")
    jstore = TelegramJobStore(env_setup / "jobs.sqlite3")
    bot = BotHandler(store=cstore, proposal_store=jstore)

    recorded_calls: list[dict[str, Any]] = []
    loop_calls: list[dict[str, Any]] = []

    def fake_completion(messages, **kwargs):
        recorded_calls.append(
            {
                "messages": list(messages),
                "profile_id": kwargs.get("profile_id"),
                "max_tokens": kwargs.get("max_tokens"),
            }
        )
        return '<tool_call>{"name": "wp_preview_page", "arguments": {"site_id": "mock-shop", "path": "/product/test/"}}</tool_call>', 0.0

    def fake_file_loop(content, messages, **kwargs):
        loop_calls.append(dict(kwargs))
        plan_id = "wr-abc123456789" if len(loop_calls) == 1 else "wr-def678901234"
        return SimpleNamespace(
            handled=True,
            content=f"Uppdaterade knappen i {plan_id}.",
            attachments=(live_png, preview_png, focus_png),
            stage_plan_id=None,
            review_stage_plan_id=plan_id,
            site_id="mock-shop",
            operation_ids=("op-1",),
            followup_cost_usd=0.0,
            model_error=False,
            calls=1,
            tool_seconds=0.1,
            active_review_meta={
                "site_id": "mock-shop",
                "plan_ids": [plan_id],
                "latest_plan_id": plan_id,
                "variant_group": "vg-btn",
                "product_id": "321",
                "preview_path": "/product/test/",
                "focus_selector": ".single_add_to_cart_button",
                "theme_slug": "azom-theme",
                "changed_files": ["style.css"],
                "preview_images": [live_png, preview_png, focus_png],
            },
        )

    monkeypatch.setattr(chat_agent, "chat_completion", fake_completion)
    monkeypatch.setattr(chat_agent, "run_bounded_file_tool_loop", fake_file_loop)
    monkeypatch.setattr(
        chat_agent,
        "_run_fast_read_loop",
        lambda *a, **k: pytest.fail("fast read loop must not run on anchored WP UI follow-up"),
    )
    monkeypatch.setattr(
        ReviewPlans,
        "get",
        lambda self, plan_id, site_id=None: {
            "plan_id": plan_id,
            "site_id": "mock-shop",
            "slug": "azom-theme",
            "variant_group": "vg-btn",
            "changed_files": ["style.css"],
            "base_sha256": "a" * 64,
            "work_sha256": "b" * 64,
        },
    )

    r1 = bot.handle(
        100,
        "Byt till grön CTA på /product/test/ på mock-shop.example.test som inringat i bilden",
        image_paths=[inbound_png],
    )
    assert "wr-abc123456789" in r1.text
    session1 = cstore.get(100)["session"]
    review1 = session1["active_wp_review"]
    assert review1["site_id"] == "mock-shop"
    assert review1["latest_plan_id"] == "wr-abc123456789"
    assert review1["preview_path"] == "/product/test/"
    assert review1["focus_selector"] == ".single_add_to_cart_button"

    # Follow-up bug report without re-sending image: must NOT trigger <clarify> and must inject active WP review + up to 2 images.
    assess = detect_change_ambiguity(
        "gör knappen större men ikonen flyttades till ny rad",
        has_wp_site_context=True,
        has_active_wp_review=True,
    )
    assert assess.is_ambiguous is False

    r2 = bot.handle(100, "gör knappen större men ikonen flyttades till ny rad")
    assert "wr-def678901234" in r2.text
    assert r2.actions is not None and any(
        "wpreview:stage:wr-def678901234" in btn.payload
        for row in r2.actions.rows
        for btn in row
    )
    assert loop_calls[-1].get("allow_wp_first") is True

    second_msgs = recorded_calls[-1]["messages"]
    tool_sys = "\n".join(
        str(m.get("content") or "") for m in second_msgs if m.get("role") == "system"
    )
    assert "Active WP review" in tool_sys
    assert "latest_plan_id=wr-abc123456789" in tool_sys
    assert "preview_path=/product/test/" in tool_sys

    attached = second_msgs[-1].get("images") or []
    assert len(attached) == 2
    assert focus_png in attached
    assert inbound_png in attached
    assert all(
        str(tmp_path) not in str(m.get("content") or "")
        for m in cstore.get(100)["messages"]
    )

    # Clear via wpreview:no:
    r_no = bot.handle_callback(100, "wpreview:no:wr-def678901234")
    assert "Avbrutet" in r_no.text
    assert "active_wp_review" not in (cstore.get(100)["session"] or {})

    # Re-persist then clear via wpreview:stage: success
    cstore.append_turn_atomic(100, session_updates={"active_wp_review": review1})
    monkeypatch.setattr(
        ReviewPlans,
        "workspace_status",
        lambda self, site_id, plan_id, actor=None: SimpleNamespace(
            ok=True, data={"changed_files": ["style.css"]}
        ),
    )
    monkeypatch.setattr(
        ReviewPlans,
        "approve_staging",
        lambda self, site_id, plan_id, actor=None, base_sha256="", work_sha256="": SimpleNamespace(
            ok=True, data={"executed": True}, message=""
        ),
    )
    r_stage = bot.handle_callback(100, "wpreview:stage:wr-def678901234")
    assert "skrivna i staging" in r_stage.text
    assert "active_wp_review" not in (cstore.get(100)["session"] or {})

    # Re-persist then clear via /new
    cstore.append_turn_atomic(100, session_updates={"active_wp_review": review1})
    bot.handle(100, "/new")
    assert "active_wp_review" not in ((cstore.get(100) or {}).get("session") or {})


def test_explicit_site_hint_injected_on_domain_mention(env_setup, monkeypatch):
    seen_messages: list[dict[str, Any]] = []
    seen_profiles: list[str | None] = []

    def fake_completion(messages, **kwargs):
        seen_messages.extend(messages)
        seen_profiles.append(kwargs.get("profile_id"))
        return "Kollar mock-shop.", 0.0

    monkeypatch.setattr(chat_agent, "chat_completion", fake_completion)
    monkeypatch.setattr(
        chat_agent,
        "_run_fast_read_loop",
        lambda *a, **k: pytest.fail("read loop should stay off for explicit WP domain change"),
    )

    res = run_chat(
        "ändra css för knapp på mock-shop.example.test",
        actor="oscar",
        clarification_confirmed=True,
    )
    assert "mock-shop" in res.text
    assert seen_profiles[0] == "wp_tool_router"
    sys_text = "\n".join(
        str(m.get("content") or "") for m in seen_messages if m.get("role") == "system"
    )
    assert "Site context hint:" in sys_text
    assert "site_id=mock-shop" in sys_text


def test_wpreview_approval_snapshot_ignores_volatile_session_keys(env_setup, monkeypatch):
    from types import SimpleNamespace

    from ecom_ops.wp_ops.review_plan import ReviewPlans

    cstore = ConversationStore(env_setup / "state.json")
    jstore = TelegramJobStore(env_setup / "jobs.sqlite3")
    bot = BotHandler(store=cstore, proposal_store=jstore)

    cstore.append_turn_atomic(
        100,
        session_updates={
            "wp_ops_context": {"actor_id": "oscar", "channel": "telegram", "conversation_id": "telegram:100", "site_id": "azom.se"},
            "wp_review_hashes": {"wr-112233445566": {"actor": "oscar", "site_id": "azom.se", "base_sha256": "a" * 64, "work_sha256": "b" * 64}},
            "last_order_id": "1001",
            "active_wp_review": {"site_id": "azom.se", "latest_plan_id": "wr-112233445566"},
        },
    )
    snap_before = TelegramJobStore.approval_snapshot(cstore.get(100))

    # Volatile keys change before deferred callback runs
    cstore.append_turn_atomic(
        100,
        session_updates={
            "last_order_id": "1009",
            "active_wp_review": {"site_id": "azom.se", "latest_plan_id": "wr-998877665544"},
            "pending_image_paths": ["/tmp/x.png"],
        },
    )
    snap_after = TelegramJobStore.approval_snapshot(cstore.get(100))
    assert snap_before == snap_after

    payload = {
        "type": "callback",
        "chat_id": "100",
        "data": "wpreview:stage:wr-112233445566",
        "approval_snapshot_required": True,
        "approval_snapshot": snap_before,
    }
    job = {"chat_id": "100", "payload": payload}
    assert bot_main._preflight_action_job(bot, job) is None

    # Mutating wp_review_hashes MUST still invalidate the approval snapshot!
    cstore.append_turn_atomic(
        100,
        session_updates={
            "wp_review_hashes": {"wr-112233445566": {"actor": "oscar", "site_id": "azom.se", "base_sha256": "c" * 64, "work_sha256": "d" * 64}},
        },
    )
    assert TelegramJobStore.approval_snapshot(cstore.get(100)) != snap_before
    err_msg = bot_main._preflight_action_job(bot, job)
    assert err_msg is not None
    assert "inaktuell" in err_msg.lower() or "bekräftelse" in err_msg.lower()

    # Re-enqueue with current snapshot succeeds when staged
    monkeypatch.setattr(
        ReviewPlans,
        "get",
        lambda self, plan_id, site_id=None: {
            "plan_id": plan_id,
            "site_id": "azom.se",
            "base_sha256": "c" * 64,
            "work_sha256": "d" * 64,
        },
    )
    monkeypatch.setattr(
        ReviewPlans,
        "workspace_status",
        lambda self, site_id, plan_id, actor=None: SimpleNamespace(ok=True, data={"changed_files": []}),
    )
    monkeypatch.setattr(
        ReviewPlans,
        "approve_staging",
        lambda self, site_id, plan_id, actor=None, base_sha256="", work_sha256="": SimpleNamespace(ok=True, data={"executed": True}, message=""),
    )
    fresh_snap = TelegramJobStore.approval_snapshot(cstore.get(100))
    assert (
        bot_main._preflight_action_job(
            bot,
            {
                "chat_id": "100",
                "payload": {
                    "type": "callback",
                    "chat_id": "100",
                    "data": "wpreview:stage:wr-112233445566",
                    "approval_snapshot_required": True,
                    "approval_snapshot": fresh_snap,
                },
            },
        )
        is None
    )
    res = bot.handle_callback(100, "wpreview:stage:wr-112233445566")
    assert "skrivna i staging" in res.text


