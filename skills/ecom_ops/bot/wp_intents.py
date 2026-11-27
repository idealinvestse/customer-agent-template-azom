"""Canonical WordPress intent, workspace, and refusal regexes shared across bot modules.

Built from named vocabulary tuples so ``chat_agent.py``, ``site_content.py``,
and ``change_clarifier.py`` stay in sync instead of drifting apart.
"""

from __future__ import annotations

import re


def _compile_word_union(*groups: tuple[str, ...], raw_prefixes: tuple[str, ...] = ()) -> re.Pattern[str]:
    word_alts = "|".join(term for group in groups for term in group)
    parts: list[str] = []
    if raw_prefixes:
        parts.extend(raw_prefixes)
    if word_alts:
        parts.append(rf"\b(?:{word_alts})\b")
    return re.compile("|".join(parts), re.I)


# ---------------------------------------------------------------------------
# Shared named vocabulary tuples
# ---------------------------------------------------------------------------

WP_FILE_PREFIXES: tuple[str, ...] = (
    r"functions\.php",
    r"style\.css",
    r"theme\.json",
    r"header\.php",
    r"footer\.php",
    r"single\.php",
    r"single-product\.php",
    r"content-single-product\.php",
    r"page\.php",
    r"front-page\.php",
    r"archive\.php",
    r"woocommerce/",
)

WP_FALLTHROUGH_PATH_PREFIXES: tuple[str, ...] = (
    r"/(?:produkt|product)/",
)

WP_CODE_TERMS: tuple[str, ...] = (
    r"[a-z0-9_-]+\.(?:php|css|scss|js|json|html)",
    r"temafil(?:er)?",
    r"pluginfil(?:er)?",
    r"temakod",
    r"pluginkod",
    r"k[äa]llkod(?:en)?",
    r"child[\s_-]?theme",
    r"barntema(?:t|n)?",
    r"modertema(?:t)?",
    r"f[öo]r[äa]ldratema(?:t)?",
    r"parent[\s_-]?theme",
    r"blocktema(?:t)?",
    r"block[\s_-]?theme",
    r"block\.json",
    r"mallhierarki",
    r"template[\s_-]?hierarch\w*",
    r"hooks?",
    r"hookar",
    r"filters?",
    r"actions?",
    r"add_action",
    r"add_filter",
    r"do_action",
    r"apply_filters",
    r"register_rest_route",
    r"register_block_type",
    r"wp_enqueue_(?:script|style)",
    r"shortcodes?",
    r"snippets?",
    r"overrides?",
    r"templates?",
    r"(?:sid|produkt|tema|block|woo(?:commerce)?-?)mall(?:ar|en)?",
    r"mallfil(?:er)?",
    r"hpos",
    r"custom_order_tables",
    r"wc_tested_up_to",
    r"mu-plugin(?:s)?",
    r"mu_plugin(?:s)?",
    r"drop-in(?:s)?",
    r"object-cache\.php",
    r"advanced-cache\.php",
    r"autouppdater\w*",
    r"auto-?update\w*",
    r"pluginuppdatering\w*",
    r"karant[äa]n\w*",
    r"quarantine",
    r"wsod",
    r"laddningsordning",
    r"(?:php|css|html|js)-?kod(?:en)?",
    r"kodbas(?:en)?",
    r"kod[äa]ndring\w*",
    r"css",
    r"php",
    r"scss",
)

WP_WORKSPACE_TERMS: tuple[str, ...] = (
    r"arbetsyta(?:n)?",
    r"workspace",
    r"refaktor(?:era|ering)",
    r"bulk[\s_-]?replace",
    r"mass[äa]ndr\w*",
    r"sajtplugin",
    r"site[\s_-]?plugin",
    r"azom-site-",
    r"scaffold",
    r"static[\s_-]?checks?",
    r"statisk(?:a)?\s+kontroll(?:er)?",
    r"sandbox",
    r"sandboxsid(?:a|an)",
    r"sandl[åa]da(?:n)?",
    r"sk[äa]rmdump(?:ar|en)?",
    r"screenshot(?:s)?",
    r"print(?:screen)?",
    r"html(?:-?kod(?:en)?)?",
    r"rendera(?:d|t|r)?",
    r"f[öo]rhandsvisning(?:en|ar)?",
    r"component[\s_-]?intel",
    r"wp\.org",
    r"wordpress\.org",
    r"playbook",
    r"wr-[a-f0-9]{4,12}",
    r"fe-[a-f0-9]{4,12}",
    r"plu-[a-f0-9]{4,12}",
    r"pau-[a-f0-9]{4,12}",
    r"plr-[a-f0-9]{4,12}",
)

WP_TOOL_PATTERNS: tuple[str, ...] = (
    r"wp_(?:open_review_workspace|list_workspaces|workspace_\w+|theme_overview|plugin_overview|"
    r"plugin_inventory|mu_plugins_and_dropins|plugin_diagnostics|"
    r"plan_plugin_updates|plan_plugin_autoupdate|plan_plugin_recovery|"
    r"bridge_health|list_translations|change_history|"
    r"code_index|template_hierarchy|component_intel|playbook|propose_\w+|read_theme_plugin_file|"
    r"list_products|get_product|list_pages|get_page|build_sample_data|fetch_page_html|preview_page|inspect_element)",
    r"wc_template_overrides_audit",
)

WP_UI_AND_PAGE_TERMS: tuple[str, ...] = (
    r"produktsid(?:a|an|or|orna|e|en|er)",
    r"product[\s_-]?page",
    r"k[öo]pknapp(?:en|ar|arna)?",
    r"(?:cta|submit|best[äa]llnings|varukorgs)-?knapp(?:en|ar|arna)?",
    r"(?:[äa]ndra|byt(?:a)?|justera|fixa|styla|g[öo]r(?:a)?)\s+(?:\w+\s+){0,2}"
    r"(?:f[äa]rg(?:en)?\s+p[åa]\s+knapp(?:en|ar)?|knapp(?:en|ens)\s+(?:f[äa]rg|storlek|stil)|"
    r"(?:marginal|ram)(?:en|er)?\s+p[åa]\s+(?:knapp|k[öo]pknapp|sid|header|footer|sektion)\w*)",
    r"ikon(?:en|er|erna)?",
    r"kundvagnsikon(?:en)?",
    r"add[\s_-]?to[\s_-]?cart",
    r"(?:css|bakgrunds|text|knapp|tema|l[äa]nk)-?f[äa]rg(?:en|er|erna)?",
    r"padding",
    r"(?:css|ytter|sid|sektion)-?marginal(?:en|er|erna)?",
    r"(?:css|kant|border)-?ram(?:en|ar|arna)?",
    r"hover",
    r"inring(?:a|ad|at|ade|ning(?:en)?)?",
    r"bifogad\s+bild",
    r"tacksid(?:a|an|or)?",
    r"thank[\s_-]?you",
    r"orderbekr[äa]ftelse-?sid(?:a|an|or)?",
    r"landningssid(?:a|an|or)?",
    r"kampanjsid(?:a|an|or)?",
    r"ny\s+sid(?:a|an)",
    r"(?:skapa|bygg(?:a)?|designa)\s+(?:en\s+|ny\s+|en\s+ny\s+)?(?:[a-zåäö0-9_-]+\s+)?sid(?:a|an)",
    r"exempelprodukt(?:er|erna)?",
    r"exempeldata",
    r"hj[äa]lpguide(?:r|n|rna)?",
)

WP_CODE_INTENT_EXTRA: tuple[str, ...] = (
    r"(?:[äa]ndra|redigera|uppdatera|byt(?:a)?|justera)\s+(?:en\s+|denna\s+|det\s+)?"
    r"(?:produkt(?:en|sida(?:n)?)?|k[öo]pknapp(?:en)?|inring\w*)",
    r"(?:skapa|bygg|utveckla|granska|fels[öo]k|analysera|kod(?:a)?|skriv(?:a)?)\s+"
    r"(?:ett\s+|en\s+|nytt\s+|detta\s+)?(?:plugin(?:et)?|till[äa]gg(?:et)?|barntema(?:t)?)",
    r"(?:uppdatera|updatera)\s+(?:alla\s+|ett\s+|det\s+|mina\s+|v[åa]ra\s+)?"
    r"(?:plugin(?:et|er|s)?|till[äa]gg(?:et|en)?)",
    r"(?:plugin|till[äa]gg)(?:et|s)?\s+(?:f[öo]r|med|som|kod|konflikt)",
    r"block(?:en|et|s)?",
)

WP_FALLTHROUGH_EXTRA: tuple[str, ...] = (
    r"(?:redigera|ändra|andra)\s+(?:en\s+|den\s+)?produkt(?:en|er|sid(?:a|an))?",
    r"(?:uppdatera|updatera)\s+(?:alla\s+|ett\s+|det\s+|mina\s+|v[åa]ra\s+)?"
    r"(?:plugin(?:et|er|s)?|till[äa]gg(?:et|en)?)",
    r"skapa\s+(?:ett\s+)?(?:nytt\s+)?(?:plugin|till[äa]gg|barntema)",
    r"granska\s+(?:plugin(?:et)?|till[äa]gg(?:et)?|tema(?:t)?|koden)",
)

# ---------------------------------------------------------------------------
# Compiled intent regexes
# ---------------------------------------------------------------------------

WP_OPS_INTENT_RE = re.compile(
    r"\b(wordpress|wordpress-sajt|wp[- ]?ops|wp-cli|plugin(?:er|s)?|tema(?:n|t|r)?|"
    r"theme(?:s)?|backup(?:s)?|säkerhets(?:fynd|skanning|koll)|uppdateringsplan|"
    r"wordpressdrift|wordpress-drift|sajt(?:en|er|erna)?|webbplats(?:en|er)?|"
    r"functions\.php|style\.css|temafil(?:er)?|pluginfil(?:er)?|arbetsyta(?:n)?|hook(?:s|ar)?|"
    r"mu-plugin(?:s)?|mu_plugin(?:s)?|drop-in(?:s)?|object-cache\.php|advanced-cache\.php|"
    r"autouppdater\w*|auto-?update\w*|pluginuppdatering\w*|karant[äa]n\w*|quarantine|"
    r"wsod|laddningsordning)\b",
    re.I,
)

WP_CODE_INTENT_RE = _compile_word_union(
    WP_CODE_TERMS,
    WP_WORKSPACE_TERMS,
    WP_TOOL_PATTERNS,
    WP_UI_AND_PAGE_TERMS,
    WP_CODE_INTENT_EXTRA,
    raw_prefixes=WP_FILE_PREFIXES,
)

WP_CODE_FALLTHROUGH_RE = _compile_word_union(
    WP_CODE_TERMS,
    WP_WORKSPACE_TERMS,
    WP_TOOL_PATTERNS,
    WP_UI_AND_PAGE_TERMS,
    WP_FALLTHROUGH_EXTRA,
    raw_prefixes=(*WP_FILE_PREFIXES, *WP_FALLTHROUGH_PATH_PREFIXES),
)

WP_WORKSPACE_INTENT_RE = _compile_word_union(
    (
        r"arbetsyta(?:n)?",
        r"workspace",
        r"sandl[åa]da(?:n)?",
        r"sandbox",
        r"sandboxsid(?:a|an)",
        r"css",
        r"f[öo]rhandsvisning(?:en|ar)?",
        r"wr-[a-f0-9]{4,12}",
        r"fe-[a-f0-9]{4,12}",
        r"wp_(?:open_review_workspace|list_workspaces|workspace_\w+|"
        r"bridge_health|list_translations|change_history|"
        r"theme_overview|plugin_overview|code_index|template_hierarchy|"
        r"read_theme_plugin_file|list_products|get_product|list_pages|get_page|"
        r"build_sample_data|fetch_page_html|preview_page|inspect_element)",
    ),
    WP_UI_AND_PAGE_TERMS,
    raw_prefixes=(r"single-product\.php", r"content-single-product\.php"),
)

VISION_WP_INTENT_RE = re.compile(
    r"\b(?:"
    r"[äa]ndra|redigera|uppdatera|byt(?:a)?|fixa|justera|ta\s+bort|l[äa]gg\s+till|"
    r"centrera|flytta|d[öo]lj|runda(?:d|de)?|st[öo]rre|mindre|"
    r"skapa|g[öo]r(?:a)?|granska|kolla|visa|inspektera|titta|"
    r"inring\w*|marker\w*|cirkel\w*|pil(?:en)?|sk[äa]rmdump\w*|screenshot\w*|print\w*|bild(?:en)?|"
    r"produkt\w*|sid(?:a|an|or|orna)|knapp\w*|k[öo]pknapp\w*|pris(?:et)?|rubrik\w*|text(?:en)?|"
    r"f[äa]rg\w*|layout\w*|design\w*|sektion\w*|banner\w*|hero|header|footer|"
    r"sandbox|sandl[åa]da\w*|f[öo]rhandsvisning\w*|preview|tema\w*|plugin\w*|html|css|mall\w*"
    r")\b",
    re.I,
)

WP_UI_FOLLOWUP_RE = re.compile(
    r"\b(?:"
    r"knapp(?:en|ar)?|k[öo]pknapp(?:en|ar)?|ikon(?:en|er)?|kundvagnsikon(?:en)?|"
    r"f[äa]rg(?:en|er)?|st[öo]rre|mindre|tydligare|snyggare|kompakt(?:are)?|"
    r"fixa|justera|bort|d[öo]lj|flytta|centrera|runda(?:d|de)?|"
    r"ram(?:en)?|border|padding|marginal(?:er)?|margin|gap|spacing|hover|"
    r"text(?:en)?|rubrik(?:en)?|storlek(?:en)?|bredd(?:en)?|h[öo]jd(?:en)?|"
    r"f[öo]rslag(?:et|en)?|variant(?:er)?|f[öo]rhandsvisning(?:en)?|preview|"
    r"rendera(?:d|t|r)?|screenshot(?:s)?|sk[äa]rmdump(?:ar|en)?|"
    r"tacksid(?:a|an)?|sid(?:a|an)|sektion(?:en|er)?|exempeldata|orderdetaljer|guider?|"
    r"layout(?:en)?|stil(?:en)?|css|style|produkt(?:sida(?:n)?)?|block(?:et)?|"
    r"sandl[åa]da(?:n)?|sandbox"
    r")\b",
    re.I,
)

WP_CONTEXT_CONTINUITY_RE = re.compile(
    r"\b(?:wordpress|wp|sajt\w*|hemsid\w*|tema\w*|plugin\w*|mu-plugin\w*|mu_plugin\w*|"
    r"drop-in\w*|autouppdater\w*|auto-?update\w*|karant[äa]n\w*|quarantine|wsod|"
    r"staging|produktion|deploy\w*|sandbox|sandl[åa]d\w*|granskning\w*|"
    r"f[öo]rhandsvisning\w*|preview|kontroll(?:er)?|"
    r"wr-[a-f0-9]{4,12}|fe-[a-f0-9]{4,12}|"
    r"plu-[a-f0-9]{4,12}|pau-[a-f0-9]{4,12}|plr-[a-f0-9]{4,12})\b",
    re.I,
)

NEW_PAGE_CREATE_RE = re.compile(
    r"\b(?:"
    r"(?:skapa|bygg(?:a)?|designa|create|build)\s+"
    r"(?:en\s+|ett\s+|helt\s+|ny(?:tt|a)?\s+)*"
    r"(?:[a-zåäö0-9_-]+\s+){0,2}"
    r"(?:sid(?:a|an)|tacksid(?:a|an)|thank[- ]?you(?:\s*sida)?|"
    r"landningssid(?:a|an)|kampanjsid(?:a|an)|undersida|uppsell(?:sid(?:a|an))?|page)|"
    r"(?:skapa|bygg(?:a)?|designa|g[öo]r(?:a)?|create|build)\s+(?:en\s+|ett\s+)?"
    r"(?:helt\s+)?ny(?:tt|a)?\s+(?:[a-zåäö0-9_-]+\s+){0,2}"
    r"(?:sid(?:a|an)|tacksid(?:a|an)|thank[- ]?you(?:\s*sida)?|"
    r"landningssid(?:a|an)|kampanjsid(?:a|an)|undersida|uppsell(?:sid(?:a|an))?|mall(?:en)?|page)|"
    r"ny\s+(?:sid(?:a|an)|tacksid(?:a|an)|thank[- ]?you(?:\s*sida)?|"
    r"landningssid(?:a|an)|kampanjsid(?:a|an)|undersida)"
    r")\b",
    re.I,
)

WP_CODE_FILE_RE = _compile_word_union(
    (
        r"[a-z0-9_-]+\.(?:php|css|scss|js|json|html)",
        r"temafil(?:er)?",
        r"pluginfil(?:er)?",
        r"temakod",
        r"pluginkod",
        r"k[äa]llkod(?:en)?",
        r"barntema(?:t|n)?",
        r"child[\s_-]?theme",
        r"snippet(?:s)?",
        r"hook(?:s|ar)?",
        r"add_action",
        r"add_filter",
        r"shortcode(?:s)?",
        r"template(?:s)?",
        r"(?:sid|produkt|tema|block|woo(?:commerce)?-?)mall(?:ar|en)?",
        r"mallfil(?:er)?",
        r"wr-[a-f0-9]{4,12}",
        r"fe-[a-f0-9]{4,12}",
        r"css",
        r"php",
        r"scss",
    ),
    raw_prefixes=WP_FILE_PREFIXES,
)

# ---------------------------------------------------------------------------
# Shared refusal patterns (site_content.py & change_clarifier.py)
# ---------------------------------------------------------------------------

WP_FILE_REFUSE_RE = re.compile(
    r"functions\.php|style\.css|\btemafil(?:er)?\b|\bpluginfil(?:er)?\b",
    re.I,
)

POST_DELETE_RE = re.compile(
    r"\b(?:radera|ta\s+bort|rensa|delete)\s+(?:ett\s+|det\s+)?(?:inl[äa]gg(?:et)?|post)\b",
    re.I,
)

POST_CREATE_RE = re.compile(
    r"\b(?:skapa|nytt)\s+(?:ett\s+)?(?:blogginl[äa]gg|inl[äa]gg|post)\b",
    re.I,
)

THEME_TOUCH_RE = re.compile(
    r"\b(?:byt|byta|aktivera|v[äa]xla|anv[äa]nd|installera|uppdatera|updatera|"
    r"radera|ta\s+bort|redigera|ändra|andra)\b.{0,40}\b(?:teman?|themes?)\b"
    r"|\b(?:tema(?:t)?|theme)\b.{0,24}\b(?:till|byta|aktivera)\b",
    re.I,
)

THEME_FALLTHROUGH_REFUSE_RE = re.compile(
    r"\b(?:byt|byta|v[äa]xla|aktivera|installera|avinstallera|"
    r"radera|ta\s+bort|delete)\s+(?:det\s+|ett\s+|nytt\s+|annat\s+|"
    r"nuvarande\s+|aktiva?\s+|mitt\s+|v[åa]rt\s+)?"
    r"(?:tema(?:t|n)?|theme)(?!\.json\b)\b"
    r"|\b(?:byt|byta|v[äa]xla|aktivera)\s+tema(?:t)?\s+till\b"
    r"|\b(?:tema(?:t)?|theme)(?!\.json\b)\s+(?:ska\s+(?:bytas|aktiveras)|byts)\b",
    re.I,
)

PACKAGE_REFUSE_RE = re.compile(
    r"\b(?:installera|avinstallera|radera|ta\s+bort|uppdatera|updatera|delete)\s+"
    r"(?:plugin(?:et|s)?|till[äa]gg(?:et)?|tema(?:t|n)?|theme)\b",
    re.I,
)

PACKAGE_FALLTHROUGH_REFUSE_RE = re.compile(
    r"\b(?:installera|avinstallera|radera|ta\s+bort|delete)\s+"
    r"(?:det\s+|ett\s+|nytt\s+|hela\s+)?"
    r"(?:plugin(?:et|s)?|till[äa]gg(?:et)?|tema(?:t|n)?|theme(?!\.json\b))\b",
    re.I,
)

SITE_REFUSAL_RE = re.compile(
    r"\b(?:"
    r"(?:installera|avinstallera|radera|ta\s+bort|delete)\s+"
    r"(?:ett\s+|det\s+|nytt\s+|hela\s+)?(?:plugin(?:et|s)?|till[äa]gg(?:et)?|tema(?:t|n)?|theme(?!\.json\b))|"
    r"(?:byt|byta|v[äa]xla)\s+(?:det\s+|ett\s+|nytt\s+|annat\s+|nuvarande\s+|aktiva?\s+)?(?:tema(?:t|n)?|theme(?!\.json\b))|"
    r"(?:radera|ta\s+bort|delete)\s+(?:ett\s+|det\s+)?(?:inl[äa]gg(?:et)?|post)|"
    r"(?:skapa|l[äa]gg\s+till)\s+(?:ett\s+)?nytt\s+(?:blogginl[äa]gg|inl[äa]gg|post)|"
    r"nytt\s+(?:blogginl[äa]gg|inl[äa]gg|post)"
    r")\b",
    re.I,
)

WP_PLUGIN_OPS_INTENT_RE = re.compile(
    r"\b(?:"
    r"pluginuppdatering\w*|autouppdater\w*|auto-?update\w*|"
    r"mu-plugin(?:s)?|mu_plugin(?:s)?|drop-in(?:s)?|"
    r"object-cache\.php|advanced-cache\.php|"
    r"wsod|karant[äa]n\w*|quarantine|"
    r"wp_plugin_\w+|wp_plan_plugin_\w+|"
    r"plu-[a-f0-9]{4,12}|pau-[a-f0-9]{4,12}|plr-[a-f0-9]{4,12}|"
    r"(?:visa|lista|kolla)\s+(?:alla\s+)?(?:plugins?|till[äa]gg)\s+(?:och|med)\s+uppdateringar|"
    r"(?:planera|skapa)\s+(?:en\s+)?(?:plugin)?uppdateringsplan"
    r")\b",
    re.I,
)

WP_PLUGIN_UPDATE_DIRECT_RE = re.compile(
    r"\b(?:uppdatera|updatera)\s+(?:alla\s+|ett\s+|det\s+|mina\s+|v[åa]ra\s+)?"
    r"(?:plugin(?:et|er|s)?|till[äa]gg(?:et|en)?)"
    r"(?:\s+(?P<slug>[a-z0-9][a-z0-9_./-]{0,80}))?\b",
    re.I,
)

_SUPPORT_ORDER_CUES_RE = re.compile(
    r"\b(?:"
    r"kunden|kund(?:en|er)?\s+(?:fick|saknar|fr[åa]gar|beh[öo]ver|klagar|undrar)|"
    r"order(?:nr|nummer)?\s*#?\d+|"
    r"bilstereo\w*|ratten|kabel(?:n)?|retursvar|reklamation|"
    r"leverans(?:en)?|sp[åa]rningsnummer|orderbekr[äa]ftelse"
    r")\b",
    re.I,
)

_EXPLICIT_WP_TECH_RE = _compile_word_union(
    WP_WORKSPACE_TERMS,
    WP_TOOL_PATTERNS,
    raw_prefixes=WP_FILE_PREFIXES,
)


def is_support_or_order_dominant(text: str) -> bool:
    """True when customer-support / order / car-multimedia cues dominate without explicit WP tech terms."""
    raw = text or ""
    if not _SUPPORT_ORDER_CUES_RE.search(raw):
        return False
    if WP_OPS_INTENT_RE.search(raw) or _EXPLICIT_WP_TECH_RE.search(raw):
        return False
    return True


def wants_wp_plugin_ops_routing(
    text: str,
    *,
    actor: str | None,
    has_wp_site_context: bool = False,
) -> bool:
    """True when plugin lifecycle/update/MU/recovery queries should route to WP ops tools."""
    raw = text or ""
    if not actor:
        return False
    try:
        from ecom_ops.rbac import Permission, resolve_actor

        actor_obj = resolve_actor(actor)
    except Exception:
        return False
    if not actor_obj.has(Permission.WP_OPS_READ):
        return False
    if WP_PLUGIN_OPS_INTENT_RE.search(raw):
        return True
    match = WP_PLUGIN_UPDATE_DIRECT_RE.search(raw)
    if not match:
        return False
    if has_wp_site_context or actor_obj.has(Permission.WP_OPS_ADMIN):
        return True
    slug = (match.group("slug") or "").strip().lower()
    if slug:
        from ecom_ops.wp_ops.plugin_policy import is_critical_plugin

        if is_critical_plugin(slug):
            return False
    return True


__all__ = [
    "NEW_PAGE_CREATE_RE",
    "PACKAGE_FALLTHROUGH_REFUSE_RE",
    "PACKAGE_REFUSE_RE",
    "POST_CREATE_RE",
    "POST_DELETE_RE",
    "SITE_REFUSAL_RE",
    "THEME_FALLTHROUGH_REFUSE_RE",
    "THEME_TOUCH_RE",
    "VISION_WP_INTENT_RE",
    "WP_CODE_FILE_RE",
    "WP_CODE_FALLTHROUGH_RE",
    "WP_CODE_INTENT_RE",
    "WP_CONTEXT_CONTINUITY_RE",
    "WP_FILE_REFUSE_RE",
    "WP_OPS_INTENT_RE",
    "WP_PLUGIN_OPS_INTENT_RE",
    "WP_PLUGIN_UPDATE_DIRECT_RE",
    "WP_UI_FOLLOWUP_RE",
    "WP_WORKSPACE_INTENT_RE",
    "is_support_or_order_dominant",
    "wants_wp_plugin_ops_routing",
]
