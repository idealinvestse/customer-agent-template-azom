"""Hybrid routing between steering a running Telegram job and starting a new parallel task."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

RouteMode = Literal["new_task", "steer"]

_TASK_CMD = re.compile(r"^/task(?:@\w+)?(?:\s+([\s\S]+))?$", re.I)
_STEER_CMD = re.compile(r"^/steer(?:@\w+)?(?:\s+([\s\S]+))?$", re.I)
_HEX_PREFIX = re.compile(r"^[0-9a-f]{4,32}$", re.I)
_RES_PREFIX = re.compile(r"^res-[0-9a-f]{4,12}$", re.I)

_NEW_TASK_PHRASE = re.compile(
    r"^(?:ny uppgift|ny task|parallellt|samtidigt|medan du|kolla (?:även|också))\b",
    re.I,
)

_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_AMOUNT_TOKEN = re.compile(
    r"\b\d{4,12}\s*(?:kr|sek|nok|dkk|eur|usd|%|dagar|timmar|minuter)\b",
    re.I,
)
_ORDER_ID_RE = re.compile(r"(?<!-)\b(\d{4,12})\b(?!-)")
_UUID_OR_CASE_HEX_RE = re.compile(
    r"\b([0-9a-f]{8})(?:-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?\b",
    re.I,
)
_EXPLICIT_CASE_ID_RE = re.compile(
    r"\b(?:ärende|case)\s*#?\s*([0-9a-f]{8})\b",
    re.I,
)

_DOMAIN_PATTERNS: dict[str, re.Pattern[str]] = {
    "order": re.compile(
        r"\b(?:order|ordrar|ordernr|ordernummer|beställning\w*|frakt\w*|spårning\w*|"
        r"spåra|tracking|leverans\w*|woo|woocommerce|paket\w*|retur\w*)\b",
        re.I,
    ),
    "cases": re.compile(
        r"\b(?:ärende\w*|case|cases|kundmail\w*|kundmejl\w*|triage\w*|inkorg\w*|"
        r"supportärende\w*|mejl\w*|mail\w*)\b",
        re.I,
    ),
    "marketing": re.compile(
        r"\b(?:ga4|google\s*ads|gads|roas|kampanj\w*|annons\w*|trafik\w*|"
        r"marketing|marknadsföring\w*|konvertering\w*)\b",
        re.I,
    ),
    "wp": re.compile(
        r"\b(?:wordpress|wp|tema\w*|theme\w*|plugin\w*|tillägg\w*|backup\w*|"
        r"sftp|startsida\w*|källkod\w*|staging|hemsida\w*|webbplats\w*)\b",
        re.I,
    ),
    "catalog": re.compile(
        r"\b(?:produkt\w*|sida|sidor|sidan|inlägg\w*|pris\w*|lager\w*|tagline|"
        r"katalog\w*|artikel\w*)\b",
        re.I,
    ),
    "research": re.compile(
        r"\b(?:research\w*|webbsök\w*|webresearch\w*|konkurrentanalys\w*|"
        r"marknadsanalys\w*|källgransk\w*|djupanalys\w*|webben)\b|\bres-[0-9a-f]{4,12}\b",
        re.I,
    ),
}

_EXACT_STEER_PHRASES = re.compile(
    r"^(?:ja|nej|ok|okej|japp|nope|absolut|precis|visst|bra|tack|kör|fortsätt|"
    r"kortare|längre|mer detaljer|mer info|sammanfatta|punktlista|"
    r"på engelska|på norska|på danska|på svenska|"
    r"och frakten|och spårningen|samma order|samma ärende)$",
    re.I,
)

_PREFIX_STEER_PHRASES = re.compile(
    r"^(?:kortare|på engelska|på norska|på danska|på svenska|"
    r"mer detaljer|och frakten|samma order|samma ärende|"
    r"fokusera på|ta med|strunta i|lägg till|glöm inte|utveckla)\b",
    re.I,
)


@dataclass(frozen=True)
class RouteDecision:
    """Routing outcome for an incoming Telegram message."""

    mode: RouteMode
    target_job_id: str | None
    cleaned_text: str
    reason: str


def _decode_json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str) and value:
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return default


def _job_payload(job: dict[str, Any]) -> dict[str, Any]:
    if isinstance(job.get("payload"), dict):
        return job["payload"]
    decoded = _decode_json(job.get("payload_json"), {})
    return decoded if isinstance(decoded, dict) else {}


def _job_text(job: dict[str, Any]) -> str:
    return str(_job_payload(job).get("text") or "")


def _job_message_ids(job: dict[str, Any]) -> set[int]:
    ids: set[int] = set()
    payload = _job_payload(job)
    for candidate in (payload.get("source_message_id"), job.get("status_message_id")):
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            ids.add(candidate)
        elif isinstance(candidate, str) and candidate.strip().isdigit():
            ids.add(int(candidate.strip()))
    raw_answers = job.get("answer_message_ids")
    if raw_answers is None:
        raw_answers = _decode_json(job.get("answer_message_ids_json"), [])
    if isinstance(raw_answers, list):
        for candidate in raw_answers:
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                ids.add(candidate)
            elif isinstance(candidate, str) and candidate.strip().isdigit():
                ids.add(int(candidate.strip()))
    return ids


def _is_steerable_kind(job: dict[str, Any]) -> bool:
    return str(job.get("kind") or "") in {"read_turn", "page_review", "research"}


def _has_steer_capacity(job: dict[str, Any], max_steers_per_job: int) -> bool:
    used = int(job.get("steer_used") or 0)
    pending = int(job.get("steer_pending") or 0)
    return (used + pending) < max_steers_per_job


def _extract_order_ids(text: str) -> set[str]:
    sanitized = _ISO_DATE.sub(" ", text)
    sanitized = _AMOUNT_TOKEN.sub(" ", sanitized)
    return set(_ORDER_ID_RE.findall(sanitized))


def _extract_case_ids(text: str) -> set[str]:
    found: set[str] = set()
    for match in _EXPLICIT_CASE_ID_RE.findall(text):
        found.add(match.lower())
    for match in _UUID_OR_CASE_HEX_RE.findall(text):
        token = match.lower()
        if any(ch in "abcdef" for ch in token):
            found.add(token)
    return found


def _detect_domains(text: str) -> set[str]:
    domains: set[str] = set()
    for name, pattern in _DOMAIN_PATTERNS.items():
        if pattern.search(text):
            domains.add(name)
    if _extract_case_ids(text):
        domains.add("cases")
    if _extract_order_ids(text) and "cases" not in domains:
        domains.add("order")
    return domains


def _is_explicit_steer_phrase(text: str) -> bool:
    normalized = " ".join(text.strip().split()).rstrip("?!.,:; ")
    if not normalized:
        return False
    return bool(
        _EXACT_STEER_PHRASES.fullmatch(normalized)
        or _PREFIX_STEER_PHRASES.match(normalized)
    )


def _match_jobs_by_prefix(
    running_jobs: list[dict[str, Any]], prefix: str
) -> list[dict[str, Any]]:
    needle = prefix.lower()
    matched: list[dict[str, Any]] = []
    for job in running_jobs:
        job_id = str(job.get("job_id") or "").lower()
        task_id = str(job.get("task_id") or "").lower()
        research_id = str(job.get("research_id") or "").lower()
        if (
            (job_id and job_id.startswith(needle))
            or (task_id and task_id.startswith(needle))
            or (research_id and research_id.startswith(needle))
        ):
            matched.append(job)
    return matched


def decide_message_routing(
    text: str,
    *,
    reply_to_message_id: int | None,
    running_jobs: list[dict[str, Any]],
    max_steers_per_job: int = 3,
) -> RouteDecision:
    """Decide whether an incoming Telegram message steers a running job or starts a new task."""
    stripped = text.strip()
    steerable_jobs = [job for job in running_jobs if _is_steerable_kind(job)]

    # 1. Explicit /task <text> always starts a new parallel task.
    task_match = _TASK_CMD.match(stripped)
    if task_match:
        cleaned = (task_match.group(1) or "").strip()
        return RouteDecision(
            mode="new_task",
            target_job_id=None,
            cleaned_text=cleaned,
            reason="explicit_task_command",
        )

    # 2. Explicit /steer <prefix> <text> or /steer <text>.
    steer_match = _STEER_CMD.match(stripped)
    if steer_match:
        body = (steer_match.group(1) or "").strip()
        if not body:
            return RouteDecision(
                mode="new_task",
                target_job_id=None,
                cleaned_text=stripped,
                reason="steer_empty",
            )
        parts = body.split(None, 1)
        if len(parts) == 2 and (
            _HEX_PREFIX.fullmatch(parts[0]) or _RES_PREFIX.fullmatch(parts[0])
        ):
            prefix, rest = parts[0], parts[1].strip()
            matches = _match_jobs_by_prefix(running_jobs, prefix)
            if len(matches) == 1:
                target = matches[0]
                if _is_steerable_kind(target) and _has_steer_capacity(target, max_steers_per_job):
                    return RouteDecision(
                        mode="steer",
                        target_job_id=str(target["job_id"]),
                        cleaned_text=rest,
                        reason="explicit_steer_prefix",
                    )
                return RouteDecision(
                    mode="new_task",
                    target_job_id=None,
                    cleaned_text=rest,
                    reason="explicit_steer_target_unavailable",
                )
            if len(matches) > 1:
                return RouteDecision(
                    mode="new_task",
                    target_job_id=None,
                    cleaned_text=body,
                    reason="explicit_steer_prefix_ambiguous",
                )
            return RouteDecision(
                mode="new_task",
                target_job_id=None,
                cleaned_text=body,
                reason="explicit_steer_prefix_not_found",
            )
        if len(steerable_jobs) == 1:
            target = steerable_jobs[0]
            if _has_steer_capacity(target, max_steers_per_job):
                return RouteDecision(
                    mode="steer",
                    target_job_id=str(target["job_id"]),
                    cleaned_text=body,
                    reason="explicit_steer_single_running",
                )
            return RouteDecision(
                mode="new_task",
                target_job_id=None,
                cleaned_text=body,
                reason="explicit_steer_limit_reached",
            )
        return RouteDecision(
            mode="new_task",
            target_job_id=None,
            cleaned_text=body,
            reason="explicit_steer_no_unique_target",
        )

    # 3. Other slash commands start as a new parallel task.
    if text.lstrip().startswith("/"):
        return RouteDecision(
            mode="new_task",
            target_job_id=None,
            cleaned_text=text,
            reason="slash_command",
        )

    if not steerable_jobs:
        return RouteDecision(
            mode="new_task",
            target_job_id=None,
            cleaned_text=text,
            reason="no_running_steerable_job",
        )

    # 4. Explicit Telegram reply matching a running job's message IDs.
    if reply_to_message_id is not None:
        reply_matches = [
            job for job in steerable_jobs if reply_to_message_id in _job_message_ids(job)
        ]
        if reply_matches:
            target = reply_matches[0]
            if _has_steer_capacity(target, max_steers_per_job):
                return RouteDecision(
                    mode="steer",
                    target_job_id=str(target["job_id"]),
                    cleaned_text=text,
                    reason="reply_to_running_job",
                )
            if target.get("kind") != "page_review":
                return RouteDecision(
                    mode="new_task",
                    target_job_id=None,
                    cleaned_text=text,
                    reason="reply_target_steer_limit",
                )
            return RouteDecision(
                mode="new_task",
                target_job_id=None,
                cleaned_text=text,
                reason="reply_page_review_steer_limit",
            )
        return RouteDecision(
            mode="new_task",
            target_job_id=None,
            cleaned_text=text,
            reason="reply_to_non_running_message",
        )

    # 5. Strict page_review isolation for unthreaded free text.
    page_reviews = [
        job
        for job in steerable_jobs
        if job.get("kind") == "page_review" and _has_steer_capacity(job, max_steers_per_job)
    ]
    if page_reviews:
        return RouteDecision(
            mode="steer",
            target_job_id=str(page_reviews[0]["job_id"]),
            cleaned_text=text,
            reason="page_review_isolation",
        )

    # 6. Hybrid intent classification for running read_turn jobs (unthreaded free text).
    read_turns = [job for job in steerable_jobs if job.get("kind") == "read_turn"]
    if not read_turns:
        return RouteDecision(
            mode="new_task",
            target_job_id=None,
            cleaned_text=text,
            reason="no_running_read_turn",
        )

    if _NEW_TASK_PHRASE.match(stripped):
        return RouteDecision(
            mode="new_task",
            target_job_id=None,
            cleaned_text=text,
            reason="explicit_new_task_phrase",
        )

    in_orders = _extract_order_ids(text)
    in_cases = _extract_case_ids(text)
    in_domains = _detect_domains(text)

    # Multiple running read_turns without reply_to_message_id: never random-steer.
    if len(read_turns) >= 2:
        matched_by_entity: list[dict[str, Any]] = []
        if in_orders or in_cases:
            for job in read_turns:
                j_text = _job_text(job)
                if (in_orders and (in_orders & _extract_order_ids(j_text))) or (
                    in_cases and (in_cases & _extract_case_ids(j_text))
                ):
                    matched_by_entity.append(job)
        if len(matched_by_entity) == 1:
            target = matched_by_entity[0]
            if _has_steer_capacity(target, max_steers_per_job):
                return RouteDecision(
                    mode="steer",
                    target_job_id=str(target["job_id"]),
                    cleaned_text=text,
                    reason="multi_running_entity_match",
                )
        return RouteDecision(
            mode="new_task",
            target_job_id=None,
            cleaned_text=text,
            reason="multi_running_no_unique_match",
        )

    # Exactly 1 running read_turn job.
    target = read_turns[0]
    if not _has_steer_capacity(target, max_steers_per_job):
        return RouteDecision(
            mode="new_task",
            target_job_id=None,
            cleaned_text=text,
            reason="steer_limit_reached",
        )

    job_text = _job_text(target)
    job_orders = _extract_order_ids(job_text)
    job_cases = _extract_case_ids(job_text)
    job_domains = _detect_domains(job_text)

    has_new_order_id = bool(in_orders) and not (in_orders & job_orders) and (
        bool(job_orders) or ("order" not in job_domains)
    )
    has_new_case_id = bool(in_cases) and not (in_cases & job_cases) and (
        bool(job_cases) or ("cases" not in job_domains)
    )

    if _is_explicit_steer_phrase(text) and not has_new_order_id and not has_new_case_id:
        return RouteDecision(
            mode="steer",
            target_job_id=str(target["job_id"]),
            cleaned_text=text,
            reason="steer_phrase",
        )

    if has_new_order_id:
        return RouteDecision(
            mode="new_task",
            target_job_id=None,
            cleaned_text=text,
            reason="different_order_id",
        )

    if has_new_case_id:
        return RouteDecision(
            mode="new_task",
            target_job_id=None,
            cleaned_text=text,
            reason="different_case_id",
        )

    if in_domains:
        if job_domains and not (in_domains & job_domains):
            return RouteDecision(
                mode="new_task",
                target_job_id=None,
                cleaned_text=text,
                reason="different_domain",
            )
        if not job_domains:
            return RouteDecision(
                mode="new_task",
                target_job_id=None,
                cleaned_text=text,
                reason="new_standalone_domain",
            )

    if len(stripped.split()) <= 4:
        return RouteDecision(
            mode="steer",
            target_job_id=str(target["job_id"]),
            cleaned_text=text,
            reason="short_followup",
        )

    return RouteDecision(
        mode="steer",
        target_job_id=str(target["job_id"]),
        cleaned_text=text,
        reason="single_running_default_steer",
    )
