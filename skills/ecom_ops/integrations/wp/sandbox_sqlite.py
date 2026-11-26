"""Synthetic SQLite schema and PHP serialization helpers for the local WordPress sandbox."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from ecom_ops.wp_ops.review_workspace import ReviewError

PLUGIN_ENTRY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,60}/[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.php$")

_SCHEMA_SQL = """
CREATE TABLE wp_options (
    option_id INTEGER PRIMARY KEY AUTOINCREMENT,
    option_name TEXT NOT NULL UNIQUE,
    option_value TEXT NOT NULL DEFAULT '',
    autoload TEXT NOT NULL DEFAULT 'yes'
);
CREATE TABLE wp_users (
    ID INTEGER PRIMARY KEY AUTOINCREMENT,
    user_login TEXT NOT NULL DEFAULT '',
    user_pass TEXT NOT NULL DEFAULT '',
    user_nicename TEXT NOT NULL DEFAULT '',
    user_email TEXT NOT NULL DEFAULT '',
    user_url TEXT NOT NULL DEFAULT '',
    user_registered TEXT NOT NULL DEFAULT '2026-01-01 00:00:00',
    user_activation_key TEXT NOT NULL DEFAULT '',
    user_status INTEGER NOT NULL DEFAULT 0,
    display_name TEXT NOT NULL DEFAULT ''
);
CREATE TABLE wp_usermeta (
    umeta_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL DEFAULT 0,
    meta_key TEXT DEFAULT NULL,
    meta_value TEXT
);
CREATE TABLE wp_posts (
    ID INTEGER PRIMARY KEY AUTOINCREMENT,
    post_author INTEGER NOT NULL DEFAULT 0,
    post_date TEXT NOT NULL DEFAULT '2026-01-01 00:00:00',
    post_date_gmt TEXT NOT NULL DEFAULT '2026-01-01 00:00:00',
    post_content TEXT NOT NULL DEFAULT '',
    post_title TEXT NOT NULL DEFAULT '',
    post_excerpt TEXT NOT NULL DEFAULT '',
    post_status TEXT NOT NULL DEFAULT 'publish',
    comment_status TEXT NOT NULL DEFAULT 'closed',
    ping_status TEXT NOT NULL DEFAULT 'closed',
    post_password TEXT NOT NULL DEFAULT '',
    post_name TEXT NOT NULL DEFAULT '',
    to_ping TEXT NOT NULL DEFAULT '',
    pinged TEXT NOT NULL DEFAULT '',
    post_modified TEXT NOT NULL DEFAULT '2026-01-01 00:00:00',
    post_modified_gmt TEXT NOT NULL DEFAULT '2026-01-01 00:00:00',
    post_content_filtered TEXT NOT NULL DEFAULT '',
    post_parent INTEGER NOT NULL DEFAULT 0,
    guid TEXT NOT NULL DEFAULT '',
    menu_order INTEGER NOT NULL DEFAULT 0,
    post_type TEXT NOT NULL DEFAULT 'post',
    post_mime_type TEXT NOT NULL DEFAULT '',
    comment_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE wp_postmeta (
    meta_id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL DEFAULT 0,
    meta_key TEXT DEFAULT NULL,
    meta_value TEXT
);
CREATE TABLE wp_terms (
    term_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT '',
    slug TEXT NOT NULL DEFAULT '',
    term_group INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE wp_termmeta (
    meta_id INTEGER PRIMARY KEY AUTOINCREMENT,
    term_id INTEGER NOT NULL DEFAULT 0,
    meta_key TEXT DEFAULT NULL,
    meta_value TEXT
);
CREATE TABLE wp_term_taxonomy (
    term_taxonomy_id INTEGER PRIMARY KEY AUTOINCREMENT,
    term_id INTEGER NOT NULL DEFAULT 0,
    taxonomy TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    parent INTEGER NOT NULL DEFAULT 0,
    count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE wp_term_relationships (
    object_id INTEGER NOT NULL DEFAULT 0,
    term_taxonomy_id INTEGER NOT NULL DEFAULT 0,
    term_order INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (object_id, term_taxonomy_id)
);
CREATE TABLE wp_comments (
    comment_ID INTEGER PRIMARY KEY AUTOINCREMENT,
    comment_post_ID INTEGER NOT NULL DEFAULT 0,
    comment_author TEXT NOT NULL DEFAULT '',
    comment_author_email TEXT NOT NULL DEFAULT '',
    comment_author_url TEXT NOT NULL DEFAULT '',
    comment_author_IP TEXT NOT NULL DEFAULT '',
    comment_date TEXT NOT NULL DEFAULT '2026-01-01 00:00:00',
    comment_date_gmt TEXT NOT NULL DEFAULT '2026-01-01 00:00:00',
    comment_content TEXT NOT NULL DEFAULT '',
    comment_karma INTEGER NOT NULL DEFAULT 0,
    comment_approved TEXT NOT NULL DEFAULT '1',
    comment_agent TEXT NOT NULL DEFAULT '',
    comment_type TEXT NOT NULL DEFAULT 'comment',
    comment_parent INTEGER NOT NULL DEFAULT 0,
    user_id INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE wp_commentmeta (
    meta_id INTEGER PRIMARY KEY AUTOINCREMENT,
    comment_id INTEGER NOT NULL DEFAULT 0,
    meta_key TEXT DEFAULT NULL,
    meta_value TEXT
);
"""


def php_serialize_list(items: list[str]) -> str:
    """Serialize a list of strings as a PHP indexed array using UTF-8 byte lengths."""
    if not items:
        return "a:0:{}"
    parts = [f'i:{idx};s:{len(item.encode("utf-8"))}:"{item}";' for idx, item in enumerate(items)]
    return f"a:{len(items)}:{{{''.join(parts)}}}"


def init_sqlite_db(
    db_path: Path,
    *,
    base_url: str,
    theme_slug: str,
    parent_slug: str | None = None,
    active_plugins: list[str] | None = None,
    site_title: str = "Azom Synthetic Fixture",
) -> None:
    """Create and seed a synthetic WordPress SQLite database for sandbox runs."""
    plugin_entries = list(active_plugins or [])
    for entry in plugin_entries:
        if not isinstance(entry, str) or not PLUGIN_ENTRY_RE.fullmatch(entry):
            raise ReviewError("PathRejected")
    serialized_plugins = php_serialize_list(plugin_entries)
    if db_path.exists():
        db_path.unlink()
    template_slug = parent_slug or theme_slug
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_SCHEMA_SQL)
        roles_serialized = (
            'a:1:{s:13:"administrator";a:2:{s:4:"name";s:13:"Administrator";'
            's:12:"capabilities";a:1:{s:4:"read";b:1;}}}'
        )
        options = [
            ("siteurl", base_url, "yes"),
            ("home", base_url, "yes"),
            ("blogname", site_title, "yes"),
            ("blogdescription", "Synthetic isolated review fixture", "yes"),
            ("users_can_register", "0", "yes"),
            ("admin_email", "synthetic@example.invalid", "yes"),
            ("start_of_week", "1", "yes"),
            ("use_balanceTags", "0", "yes"),
            ("use_smilies", "0", "yes"),
            ("require_name_email", "1", "yes"),
            ("comments_notify", "0", "yes"),
            ("posts_per_rss", "10", "yes"),
            ("rss_use_excerpt", "0", "yes"),
            ("blog_charset", "UTF-8", "yes"),
            ("html_type", "text/html", "yes"),
            ("default_category", "1", "yes"),
            ("default_comment_status", "closed", "yes"),
            ("default_ping_status", "closed", "yes"),
            ("posts_per_page", "10", "yes"),
            ("date_format", "Y-m-d", "yes"),
            ("time_format", "H:i", "yes"),
            ("permalink_structure", "", "yes"),
            ("blog_public", "0", "yes"),
            ("template", template_slug, "yes"),
            ("stylesheet", theme_slug, "yes"),
            ("active_plugins", serialized_plugins, "yes"),
            ("show_on_front", "page", "yes"),
            ("page_on_front", "2", "yes"),
            ("page_for_posts", "0", "yes"),
            ("WPLANG", "", "yes"),
            ("timezone_string", "UTC", "yes"),
            ("gmt_offset", "0", "yes"),
            ("db_version", "61245", "yes"),
            ("initial_db_version", "61245", "yes"),
            ("wp_user_roles", roles_serialized, "yes"),
            ("cron", "a:0:{}", "yes"),
            ("rewrite_rules", "", "yes"),
        ]
        conn.executemany(
            "INSERT INTO wp_options (option_name, option_value, autoload) VALUES (?, ?, ?)",
            options,
        )
        conn.execute(
            "INSERT INTO wp_users (ID, user_login, user_pass, user_nicename, user_email, display_name) "
            "VALUES (1, 'synthetic', '', 'synthetic', 'synthetic@example.invalid', 'Synthetic Fixture')"
        )
        conn.execute("INSERT INTO wp_terms (term_id, name, slug) VALUES (1, 'Uncategorized', 'uncategorized')")
        conn.execute(
            "INSERT INTO wp_term_taxonomy (term_taxonomy_id, term_id, taxonomy, count) "
            "VALUES (1, 1, 'category', 0)"
        )
        conn.execute(
            """
            INSERT INTO wp_posts (
                ID, post_author, post_content, post_title, post_status,
                post_name, guid, post_type
            ) VALUES (
                2, 1,
                '<p class="azom-fixture-content">Synthetic WordPress homepage fixture.</p>',
                'Synthetic Fixture Home',
                'publish',
                'synthetic-home',
                ?,
                'page'
            )
            """,
            (f"{base_url}/?page_id=2",),
        )
        conn.commit()
    finally:
        conn.close()


_php_serialize_list = php_serialize_list
_init_sqlite_db = init_sqlite_db
