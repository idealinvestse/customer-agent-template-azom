"""Swedish operator and agent playbooks for WordPress/WooCommerce engineering in chat.

Pure in-memory catalog and prompt matcher with no IO, network, or permission
decisions. Selecting or reading a playbook never grants site access or bypasses
HITL staging and production gates.
"""

from __future__ import annotations

import re
from typing import Any

PLAYBOOK_TOPICS: tuple[str, ...] = (
    "child_theme_edit",
    "theme_json_design",
    "template_hierarchy_debug",
    "woo_template_overrides",
    "plugin_hooks_and_compat",
    "site_plugin_scaffold",
    "bulk_refactor",
    "static_and_sandbox_qa",
    "staging_and_hitl",
    "component_intel_and_audit",
    "new_page_and_sample_data",
    "live_overlay_ui_iteration",
)

PLAYBOOKS: dict[str, dict[str, Any]] = {
    "child_theme_edit": {
        "topic": "child_theme_edit",
        "title": "Barntema först (child theme edit)",
        "summary": (
            "Ändra alltid i det aktiva barntemat eller i en isolerad wr-*-arbetsyta. "
            "Föräldratemat är endast skrivskyddat underlag och får inte ändras direkt."
        ),
        "steps": [
            "Kör wp_theme_overview för att verifiera aktivt barntema, modertema och theme_kind.",
            "Öppna en wr-*-arbetsyta med wp_open_review_workspace(kind='theme').",
            "Behöver du utgå från en modertemafil, kopiera in den till arbetsytan med wp_workspace_copy_from_parent.",
            "Gör små, avgränsade diffar med wp_workspace_patch_file eller wp_workspace_apply_changes.",
            "Kör wp_workspace_static_checks och wp_workspace_run_checks före begäran om staging.",
        ],
        "tools": [
            "wp_theme_overview",
            "wp_open_review_workspace",
            "wp_workspace_copy_from_parent",
            "wp_workspace_patch_file",
            "wp_workspace_apply_changes",
            "wp_workspace_static_checks",
            "wp_workspace_run_checks",
        ],
        "anti_patterns": [
            "Redigera aldrig föräldratemat direkt när ett barntema finns eller kan användas.",
            "Gissa aldrig filinnehåll utan att ha läst eller verifierat målsökvägen först.",
            "Lägg inte affärslogik i temats functions.php om den hör hemma i ett sajtplugin.",
        ],
    },
    "theme_json_design": {
        "topic": "theme_json_design",
        "title": "Design och tokens i theme.json (v2/v3)",
        "summary": (
            "Använd strukturerade JSON-pekare via wp_workspace_theme_json för färger, "
            "typografi och blockinställningar i stället för sköra text- eller regex-ersättningar."
        ),
        "steps": [
            "Verifiera med wp_theme_overview att temat använder theme.json (blocktema eller hybrid) och kontrollera version/schema.",
            "Läs befintlig theme.json med wp_read_theme_plugin_file (Oscar) eller via arbetsytan innan du ändrar nycklar.",
            "Öppna en wr-*-arbetsyta och uppdatera värden med wp_workspace_theme_json (set eller remove med exakta JSON pointers).",
            "Validera JSON-syntax och designregler med wp_workspace_static_checks samt visuell sandbox med wp_workspace_run_checks.",
        ],
        "tools": [
            "wp_theme_overview",
            "wp_read_theme_plugin_file",
            "wp_open_review_workspace",
            "wp_workspace_theme_json",
            "wp_workspace_static_checks",
            "wp_workspace_run_checks",
        ],
        "anti_patterns": [
            "Använd inte rå regex-ersättning eller lös strängklippning på theme.json.",
            "Välj aldrig theme.json som aktiv sidmall; theme.json är design- och konfigurationsinventarie.",
            "Skicka inte staging-begäran innan JSON-validering och static checks är körda.",
        ],
    },
    "template_hierarchy_debug": {
        "topic": "template_hierarchy_debug",
        "title": "Felsökning av WordPress mallhierarki",
        "summary": (
            "Fastställ exakt vilken mall som faktiskt gäller för startsidan eller en namngiven sida "
            "innan du skapar eller ändrar mallfiler."
        ),
        "steps": [
            "Kör wp_template_hierarchy för att få serverns verifierade kandidatordning för aktivt tema.",
            "Starta sidgranskning ('granska startsidans källkod' eller 'granska sidan <slug|id> källkod', som kör wp_page_review_home, wp_page_review_content, wp_page_review_sources och wp_page_review_preview) för att se databastilldelning och Site Editor-mallar.",
            "Kontrollera hooked styrning med wp_code_index (t.ex. template_include, template_redirect, pre_get_posts).",
            "Skilj strikt på databaslager (content.raw), källfilslager (source_file) och renderad DOM.",
        ],
        "tools": [
            "wp_template_hierarchy",
            "wp_page_review_home",
            "wp_page_review_sources",
            "wp_theme_overview",
            "wp_code_index",
            "wp_read_theme_plugin_file",
        ],
        "anti_patterns": [
            "Gissa aldrig att page.php eller front-page.php används utan att kontrollera show_on_front och hierarkin.",
            "Blanda inte ihop blockteman (templates/*.html) med klassiska PHP-mallar.",
            "Påstå inte att en template assignment är verifierad förrän källfilen faktiskt har lästs.",
        ],
    },
    "woo_template_overrides": {
        "topic": "woo_template_overrides",
        "title": "WooCommerce template overrides och versionsdrift",
        "summary": (
            "Granska alltid @version-drift i temats woocommerce/-katalog och kopiera endast rena "
            "mallar in i wr-*-arbetsytan med extra försiktighet kring kassa, varukorg och e-post."
        ),
        "steps": [
            "Kör wc_template_overrides_audit för att hitta temats woocommerce/-overrides och kontrollera @version-drift.",
            "Öppna en wr-*-arbetsyta på barntemat och kopiera saknad eller uppdaterad mall med wp_workspace_copy_woo_template.",
            "Jämför ändringen noggrant mot Woo-mallens hooks så att inga nödvändiga actions/filters tappas bort.",
            "Var extra försiktig med cart-, checkout- och e-postmallar (hög risk; staging-knappen kan blockeras och kräva Oscar).",
            "Kör wp_workspace_static_checks och wp_workspace_run_checks efter ändring.",
        ],
        "tools": [
            "wc_template_overrides_audit",
            "wp_theme_overview",
            "wp_open_review_workspace",
            "wp_workspace_copy_woo_template",
            "wp_workspace_patch_file",
            "wp_workspace_static_checks",
            "wp_workspace_run_checks",
        ],
        "anti_patterns": [
            "Patcha aldrig woocommerce-pluginets egna filer direkt.",
            "Ersätt inte en enkel action-hook med en hel template override om en hook räcker.",
            "Gissa aldrig att en override är uppdaterad utan att ha kontrollerat @version i wc_template_overrides_audit.",
        ],
    },
    "plugin_hooks_and_compat": {
        "topic": "plugin_hooks_and_compat",
        "title": "Plugin-hooks, tredjepartsgränser och Woo HPOS",
        "summary": (
            "Inspektera tredjepartsplugins skrivskyddat, upptäck hooks med wp_code_index "
            "och lägg anpassningar i sajtplugin eller barntema med HPOS-stöd."
        ),
        "steps": [
            "Inspektera tredjepartspluginet skrivskyddat med wp_plugin_overview (Oscar) och wp_code_index (add_action/add_filter/REST/block).",
            "Placera nya kopplingar och affärslogik i sajtpluginet azom-site-<site_id> eller i barntemats hooks.",
            "Säkerställ WooCommerce HPOS-kompatibilitet (woo_compat_declaration för custom_order_tables och wc_get_order i stället för shop_order).",
            "Kör wp_workspace_static_checks för att fånga saknad nonce, capability-kontroll, escaping eller HPOS-varningar.",
        ],
        "tools": [
            "wp_plugin_overview",
            "wp_code_index",
            "wp_search_component",
            "wp_open_review_workspace",
            "wp_workspace_scaffold",
            "wp_workspace_static_checks",
        ],
        "anti_patterns": [
            "Redigera eller forka aldrig tredjepartsplugins eller WooCommerce core direkt.",
            "Använd inte direkta shop_order-postantaganden när HPOS används.",
            "Utelämna aldrig current_user_can, nonce-verifiering eller context-escaping på skrivande endpoints/formulär.",
        ],
    },
    "site_plugin_scaffold": {
        "topic": "site_plugin_scaffold",
        "title": "Skapa sajtplugin eller barntemagrund (scaffold)",
        "summary": (
            "Använd wp_workspace_scaffold för att lägga återanvändbar sajtlogik i ett litet "
            "sajtplugin (azom-site-<site_id>) i stället för att överbelasta functions.php."
        ),
        "steps": [
            "Avgör om behovet är presentation (barntema) eller funktionalitet/integration/hooks (sajtplugin azom-site-<site_id>).",
            "Öppna en wr-*-arbetsyta med wp_open_review_workspace.",
            "Kör wp_workspace_scaffold med lämplig mall: child_theme_basics, site_plugin_basics eller woo_compat_declaration.",
            "Komplettera filer med wp_workspace_create_file/wp_workspace_patch_file och kontrollera med wp_workspace_static_checks.",
        ],
        "tools": [
            "wp_open_review_workspace",
            "wp_workspace_scaffold",
            "wp_workspace_create_file",
            "wp_workspace_patch_file",
            "wp_workspace_static_checks",
            "wp_workspace_run_checks",
        ],
        "anti_patterns": [
            "Lägg inte CPT-registrering, Woo-integrationer eller affärsregler i functions.php när de hör hemma i azom-site-<site_id>.",
            "Skapa inga fristående filer i produktions- eller stagingträdet direkt utanför wr-*-arbetsytan.",
        ],
    },
    "bulk_refactor": {
        "topic": "bulk_refactor",
        "title": "Övergripande refaktorering i flera filer",
        "summary": (
            "Kartlägg alla träffar först, kör dry_run=True innan verklig bulk-ersättning och "
            "granska den samlade diffen i wr-*-arbetsytan."
        ),
        "steps": [
            "Kartlägg symboler, hooks och träffar med wp_code_index och wp_search_component (stöd för regex och plan_id).",
            "Öppna en wr-*-arbetsyta och kör wp_workspace_bulk_replace med dry_run=True först för att verifiera exakt träfflista.",
            "Applicera med wp_workspace_bulk_replace(dry_run=False) eller atomiskt via wp_workspace_apply_changes.",
            "Inspektera resultatet med wp_workspace_diff och verifiera med wp_workspace_static_checks samt wp_workspace_run_checks.",
        ],
        "tools": [
            "wp_code_index",
            "wp_search_component",
            "wp_open_review_workspace",
            "wp_workspace_bulk_replace",
            "wp_workspace_apply_changes",
            "wp_workspace_diff",
            "wp_workspace_static_checks",
            "wp_workspace_run_checks",
        ],
        "anti_patterns": [
            "Kör aldrig wp_workspace_bulk_replace med dry_run=False utan en föregående dry_run=True-granskning.",
            "Gör inte blinda globala ersättningar över vendor, node_modules eller orelaterade moduler.",
        ],
    },
    "static_and_sandbox_qa": {
        "topic": "static_and_sandbox_qa",
        "title": "Statisk QA, sandbox och page_review före staging",
        "summary": (
            "Kör snabb statisk säkerhets-/syntax-/JSON-kontroll och serverns check-katalog "
            "innan du ber om staging, och redovisa alltid ej körda kontroller ärligt."
        ),
        "steps": [
            "Granska arbetsytans ändringar med wp_workspace_status och wp_workspace_diff.",
            "Kör wp_workspace_static_checks för snabb säkerhets-, PHP-syntax-, Woo- och JSON-lint.",
            "Kör wp_workspace_run_checks för att köra check-katalogen (och sandbox/smoke när aktiverat) och bind ev. page_review.",
            "Kontrollera att check_run.published är true för aktuell hash innan staging begärs.",
        ],
        "tools": [
            "wp_workspace_status",
            "wp_workspace_diff",
            "wp_workspace_static_checks",
            "wp_workspace_run_checks",
            "wp_page_review_home",
            "wp_page_review_content",
            "wp_page_review_sources",
            "wp_page_review_preview",
        ],
        "anti_patterns": [
            "Påstå aldrig att en check med status unavailable, not_run eller simulated har passerat.",
            "Begär inte staging på en föråldrad revision efter nya filändringar utan att köra om checks.",
        ],
    },
    "staging_and_hitl": {
        "topic": "staging_and_hitl",
        "title": "Stagingknappar och mänskligt godkännande (fe-* vs wr-*)",
        "summary": (
            "Chatten skriver aldrig direkt till sajten. Enstaka filplaner (fe-*) och "
            "flerfils-arbetsytor (wr-*) använder skilda serverknappar, och produktion kräver Oscar."
        ),
        "steps": [
            "För en enstaka enkel filändring: använd fe-* via wp_propose_file_diff/wp_propose_new_file; staging kräver serverknappen wpfile:stage:fe-....",
            "För tema-/pluginarbete i arbetsyta (wr-*): färdigställ ändringar + checks i wr-*-arbetsytan; staging kräver serverknappen wpreview:stage:wr-....",
            "Förklara att ett fritt 'ja' eller 'godkänn' i chatten aldrig utför en skrivning.",
            "Eskalera högriskändringar (kassa, varukorg, betalning, frakt, order, lager) och all produktionsdeploy till Oscar; produktion är stängd tills verifierad backup och återställning finns.",
        ],
        "tools": [
            "wp_workspace_status",
            "wp_workspace_static_checks",
            "wp_workspace_run_checks",
            "wp_propose_file_diff",
            "wp_propose_new_file",
            "wp_apply_staging",
            "wp_deploy_production",
        ],
        "anti_patterns": [
            "Blanda aldrig ihop fe-*-planer (wpfile:stage:fe-...) med wr-*-arbetsytor (wpreview:stage:wr-...).",
            "Påstå aldrig att staging eller produktion har uppdaterats innan serverns knapp/POST returnerat utfört resultat.",
        ],
    },
    "component_intel_and_audit": {
        "topic": "component_intel_and_audit",
        "title": "Komponentöversikt och valfri extern intel",
        "summary": (
            "Börja alltid med sajtens lokala komponentöversikter innan filträdsgrävning. "
            "Extern WordPress.org-intel är strikt valfri och avstängd som standard."
        ),
        "steps": [
            "Lista sajtens registrerade komponenter med wp_list_components(kind='theme'|'plugin').",
            "Hämta struktur, barntema-relation och hooks med wp_theme_overview eller wp_plugin_overview (Oscar).",
            "Anropa wp_component_intel endast när uppgiften uttryckligen behöver WordPress.org-metadata och AZOM_WPORG_INTEL=1 är påslaget.",
            "Fortsätt med wp_code_index eller wp_search_component endast på den bekräftade komponenten.",
        ],
        "tools": [
            "wp_list_components",
            "wp_theme_overview",
            "wp_plugin_overview",
            "wp_component_intel",
            "wp_code_index",
        ],
        "anti_patterns": [
            "Kör aldrig spekulativa externa anrop eller anta att AZOM_WPORG_INTEL=1 är aktiverat.",
            "Börja aldrig läsa godtyckliga filer innan wp_list_components eller overview visat exakt slug.",
        ],
    },
    "new_page_and_sample_data": {
        "topic": "new_page_and_sample_data",
        "title": "Ny sida med syntetisk exempeldata (page_draft)",
        "summary": (
            "Bygg helt nya sidor (t.ex. tacksida, landningssida eller kampanjsida) som "
            "work/page_draft.json med syntetisk exempeldata i sajtens riktiga header/footer-skal."
        ),
        "steps": [
            "Hämta befintliga guider och syntetisk order-/produktdata via wp_list_pages och wp_build_sample_data (noll kund-PII).",
            "Öppna en wr-*-arbetsyta med wp_open_review_workspace och spara sidutkastet via wp_workspace_page_draft.",
            "Rendera fristående förhandsvisning genom att anropa wp_preview_page(plan_id=...) och utelämna path (eller använd publik shell_path som '/').",
            "Presentera skärmbilder och interaktiv sandbox-länk innan några temafiler föreslås för staging.",
        ],
        "tools": [
            "wp_list_pages",
            "wp_build_sample_data",
            "wp_open_review_workspace",
            "wp_workspace_page_draft",
            "wp_preview_page",
        ],
        "anti_patterns": [
            "Lägg aldrig in riktiga kundmejl, telefonnummer eller personnummer i page_draft.json — använd alltid syntetiska adresser (test@example.invalid).",
            "Försök aldrig hämta /checkout, /checkout/order-received/, /cart eller /varukorg som preview-path; utelämna path för fristående page_draft-preview.",
        ],
    },
    "live_overlay_ui_iteration": {
        "topic": "live_overlay_ui_iteration",
        "title": "Iterativ UI/CSS-justering på befintlig wr-*-arbetsyta",
        "summary": (
            "Fortsätt på en aktiv wr-*-arbetsyta vid visuella finjusteringar (knappar, färg, "
            "marginaler, typografi) istället för att börja om från live."
        ),
        "steps": [
            "Inspektera målsektionen eller knappen med wp_inspect_element(plan_id=..., selector=...) för att se exakt DOM och beräknad CSS.",
            "Justera direkt i den aktiva wr-*-arbetsytan eller skapa en A/B-variant med wp_workspace_fork och samma variant_group.",
            "Verifiera snabbt med wp_preview_page(plan_id=..., reuse_live_baseline=True, focus_selector=...) och granska visual_health-varningar.",
            "Bekräfta att inga CSS-kollisioner, avklippta texter eller nollkontrast uppstår på både desktop och mobil.",
        ],
        "tools": [
            "wp_inspect_element",
            "wp_workspace_fork",
            "wp_workspace_patch_file",
            "wp_preview_page",
        ],
        "anti_patterns": [
            "Öppna inte en ny ogrupperad wr-*-arbetsyta från live när användaren ber om en följdjustering eller A/B-variant.",
            "Gissa aldrig CSS-selektorer blint utan att först kontrollera verklig DOM/CSS med wp_inspect_element eller wp_search_component.",
        ],
    },
}

_MIN_MATCH_SCORE = 2

_TOPIC_RULES: dict[str, tuple[tuple[re.Pattern[str], int], ...]] = {
    "child_theme_edit": (
        (re.compile(r"\b(?:child[\s_-]?theme|barntema|child[\s_-]?tema)\b", re.I), 4),
        (re.compile(r"\b(?:parent[\s_-]?theme|f[öo]r[äa]ldratema|modertema)\b", re.I), 4),
        (re.compile(r"wp_workspace_copy_from_parent|wp_theme_overview", re.I), 4),
        (re.compile(r"\b(?:style\.css|header\.php|footer\.php|single\.php|archive\.php)\b", re.I), 3),
        (re.compile(r"\bfunctions\.php\b", re.I), 2),
        (re.compile(r"\b(?:tema[äa]ndring|theme[\s_-]?edit|[äa]ndra\s+temat|redigera\s+temat|css|styling)\b", re.I), 2),
    ),
    "theme_json_design": (
        (re.compile(r"theme\.json|wp_workspace_theme_json", re.I), 5),
        (re.compile(r"\b(?:json[\s_-]?pointer|global\s+styles|design\s+tokens?|block\s+settings)\b", re.I), 4),
        (re.compile(r"\b(?:f[äa]rgpalett|color\s+palette|typography|typografi|styles\.color|spacing)\b", re.I), 3),
    ),
    "template_hierarchy_debug": (
        (re.compile(r"\b(?:template[\s_-]?hierarchy|mallhierarki|wp_template_hierarchy)\b", re.I), 5),
        (re.compile(r"\b(?:list_templates|site[\s_-]?editor|block\s+template|sidmall)\b", re.I), 4),
        (re.compile(r"\b(?:vilken\s+mall|which\s+template|front-page\.php|template_include|wp_page_review_home|wp_page_review_content|wp_page_review_sources|wp_page_review_preview)\b", re.I), 4),
        (re.compile(r"\b(?:templates/[\w.-]+\.html|startsidan(?:s)?\s+mall)\b", re.I), 3),
    ),
    "woo_template_overrides": (
        (re.compile(r"wc_template_overrides_audit|wp_workspace_copy_woo_template", re.I), 5),
        (re.compile(r"\b(?:woo(?:commerce)?[\s_-]*(?:template|mall|override)s?|@version|utdaterad(?:e)?\s+mall(?:ar)?)\b", re.I), 5),
        (re.compile(r"woocommerce/", re.I), 4),
        (re.compile(r"\b(?:email[\s_-]?template|e-?postmall|order-pay|thankyou)\b", re.I), 3),
        (re.compile(r"\b(?:woocommerce)\b", re.I), 1),
        (re.compile(r"\b(?:checkout|kassan?|cart|varukorg)\b", re.I), 1),
    ),
    "plugin_hooks_and_compat": (
        (re.compile(r"\b(?:hpos|custom_order_tables|woo_compat_declaration)\b", re.I), 5),
        (re.compile(r"\b(?:add_action|add_filter|do_action|apply_filters|register_rest_route|register_block_type)\b", re.I), 4),
        (re.compile(r"\b(?:tredjepartsplugin|third[\s_-]?party\s+plugin|pluginkonflikt|plugin\s+conflict|wp_code_index)\b", re.I), 4),
        (re.compile(r"\b(?:hooks?|kompatibilitet|compatibility)\b", re.I), 3),
    ),
    "site_plugin_scaffold": (
        (re.compile(r"\b(?:wp_workspace_scaffold|site_plugin_basics|child_theme_basics)\b", re.I), 5),
        (re.compile(r"\b(?:site[\s_-]?plugin|sajtplugin|azom-site-|scaffold|skapa\s+(?:ett\s+)?(?:nytt\s+)?plugin)\b", re.I), 4),
        (re.compile(r"\b(?:ist[äa]llet\s+f[öo]r\s+functions\.php|egen\s+plugin|nytt\s+barntema)\b", re.I), 4),
    ),
    "bulk_refactor": (
        (re.compile(r"wp_workspace_bulk_replace|wp_workspace_apply_changes|wp_workspace_diff", re.I), 5),
        (re.compile(r"\b(?:bulk[\s_-]?refactor|bulk[\s_-]?replace|refaktor(?:era|ering)|mass[äa]ndr(?:a|ing))\b", re.I), 5),
        (re.compile(r"\b(?:dry_run|multi[\s_-]?file|flera\s+filer|ers[äa]tt\s+i\s+alla|rename\s+prefix|byt\s+prefix)\b", re.I), 4),
        (re.compile(r"\b(?:regex)\b", re.I), 2),
    ),
    "static_and_sandbox_qa": (
        (re.compile(r"wp_workspace_static_checks|wp_workspace_run_checks", re.I), 5),
        (re.compile(r"\b(?:static[\s_-]?checks?|statisk(?:a)?\s+kontroll(?:er)?|php_lint|php\s+-l|woo_static)\b", re.I), 4),
        (re.compile(r"\b(?:sandbox|smoke[\s_-]?test|json\s+lint|verifiera\s+[äa]ndring|testa\s+[äa]ndring)\b", re.I), 3),
        (re.compile(r"\b(?:lint|qa|nonce|escaping|s[äa]kerhetskontroll)\b", re.I), 2),
    ),
    "staging_and_hitl": (
        (re.compile(r"wpreview:stage|wpfile:stage|\bfe-[a-f0-9]{4,12}\b|\bwr-[a-f0-9]{4,12}\b", re.I), 5),
        (re.compile(r"\b(?:staging|production\s+deploy|produktionsdeploy|rulla\s+ut|rollback|hitl)\b", re.I), 4),
        (re.compile(r"\b(?:wp_apply_staging|wp_deploy_production|godk[äa]nnandeknapp|stagingknapp)\b", re.I), 4),
        (re.compile(r"\b(?:produktion|deploy|publicera|eskalera\s+till\s+oscar)\b", re.I), 2),
    ),
    "component_intel_and_audit": (
        (re.compile(r"wp_component_intel|wp_list_components|wp_plugin_overview|AZOM_WPORG_INTEL", re.I), 5),
        (re.compile(r"\b(?:component[\s_-]?intel|wordpress\.org|wp\.org|lista\s+(?:teman|plugins)|vilka\s+(?:teman|plugins))\b", re.I), 4),
        (re.compile(r"\b(?:inventer(?:a|ing)|komponent[öo]versikt|component\s+audit)\b", re.I), 3),
    ),
    "new_page_and_sample_data": (
        (re.compile(r"wp_workspace_page_draft|wp_build_sample_data|page_draft\.json", re.I), 5),
        (re.compile(r"\b(?:tacksida|tack-?sida|thank[\s_-]?you[\s_-]?page|order[\s_-]?confirmation|orderbekr[äa]ftelse)\b", re.I), 5),
        (re.compile(r"\b(?:ny\s+(?:sida|landningssida|kampanjsida)|skapa\s+en\s+ny\s+sida|designa\s+en\s+ny\s+sida|landing[\s_-]?page)\b", re.I), 4),
        (re.compile(r"\b(?:exempeldata|syntetisk\s+data|sample[\s_-]?data|standalone[\s_-]?page)\b", re.I), 3),
    ),
    "live_overlay_ui_iteration": (
        (re.compile(r"wp_inspect_element|wp_workspace_fork|variant_group|reuse_live_baseline", re.I), 5),
        (re.compile(r"\b(?:a/b-?variant|tv[åa]\s+varianter|f[öo]rslag\s+b|f[öo]ljdjustering|finjustera\s+(?:knapp|knappen|färg|marginal))\b", re.I), 4),
        (re.compile(r"\b(?:k[öo]pknapp(?:en)?|l[äa]gg\s+i\s+varukorg|add[\s_-]?to[\s_-]?cart|cta-?knapp(?:en)?)\b", re.I), 3),
        (re.compile(r"\b(?:f[öo]rhandsvisning|live[\s_-]?overlay|computed[\s_-]?styles?|visual_health)\b", re.I), 3),
    ),
}


def _copy_playbook(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "topic": entry["topic"],
        "title": entry["title"],
        "summary": entry["summary"],
        "steps": list(entry["steps"]),
        "tools": list(entry["tools"]),
        "anti_patterns": list(entry["anti_patterns"]),
    }


def get_playbook(topic: str) -> dict[str, Any]:
    """Return one playbook by topic, or an error dict with available_topics."""
    key = str(topic or "").strip().lower()
    entry = PLAYBOOKS.get(key)
    if entry is None:
        return {
            "ok": False,
            "error": f"Okänt playbook-ämne: {topic!r}",
            "available_topics": list(PLAYBOOK_TOPICS),
        }
    return _copy_playbook(entry)


def select_playbooks(prompt: str, max_topics: int = 2) -> list[dict[str, Any]]:
    """Select up to ``max_topics`` relevant playbooks for a Swedish/English prompt."""
    text = str(prompt or "").strip()
    if not text or max_topics <= 0:
        return []
    scored: list[tuple[int, int, str]] = []
    for index, topic in enumerate(PLAYBOOK_TOPICS):
        rules = _TOPIC_RULES.get(topic, ())
        score = sum(weight for pattern, weight in rules if pattern.search(text))
        if score >= _MIN_MATCH_SCORE:
            scored.append((-score, index, topic))
    scored.sort()
    return [_copy_playbook(PLAYBOOKS[topic]) for _neg_score, _idx, topic in scored[:max_topics]]


def _format_one_hint(playbook: dict[str, Any]) -> str:
    tools = ", ".join(playbook["tools"][:4])
    first_step = playbook["steps"][0] if playbook["steps"] else playbook["summary"]
    anti = playbook["anti_patterns"][0] if playbook["anti_patterns"] else ""
    parts = [f"- [{playbook['topic']}] {playbook['title']}: {first_step}"]
    if tools:
        parts.append(f"  Verktyg: {tools}.")
    if anti:
        parts.append(f"  Undvik: {anti}")
    return "\n".join(parts)


def format_playbook_hints(
    prompt: str,
    max_topics: int = 2,
    max_chars: int = 900,
) -> str:
    """Format compact Swedish playbook hints within ``max_chars`` for prompt injection."""
    if max_chars <= 0:
        return ""
    selected = select_playbooks(prompt, max_topics=max_topics)
    if not selected:
        return ""
    header = "Relevanta WP/Woo-playbooks (endast vägledning, ger ingen behörighet):"
    lines = [header]
    for playbook in selected:
        block = _format_one_hint(playbook)
        candidate = "\n".join([*lines, block])
        if len(candidate) <= max_chars:
            lines.append(block)
        elif len(lines) == 1:
            # Always emit a compact hint for the top topic when max_chars is tight.
            compact = f"- [{playbook['topic']}] {playbook['title']}: {playbook['summary']}"
            lines.append(compact)
            break
        else:
            break
    rendered = "\n".join(lines)
    if len(rendered) <= max_chars:
        return rendered
    if max_chars <= 3:
        return rendered[:max_chars]
    cut = rendered[: max_chars - 3].rstrip()
    return f"{cut}..."


__all__ = [
    "PLAYBOOKS",
    "PLAYBOOK_TOPICS",
    "format_playbook_hints",
    "get_playbook",
    "select_playbooks",
]
