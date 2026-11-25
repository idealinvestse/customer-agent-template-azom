"""Opt-in, loopback-only local WordPress + PHP runtime for synthetic theme review.

This module never runs unless ``AZOM_WP_SANDBOX=1`` is explicitly enabled and
the pinned WordPress/PHP runtime manifest passes ``check_runtime``. It binds
exclusively to ``127.0.0.1`` on an ephemeral port, never mounts a Docker socket
or broad host path, never modifies the fetched WordPress core tree, and never
copies host secrets into the PHP child environment.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ecom_ops.integrations.wp_runtime_fetch import RuntimeFetchError, check_runtime, default_dest
from ecom_ops.wp_ops.review_runner import FORBIDDEN_ENV_NAMES, SandboxGate, sandbox_enabled
from ecom_ops.wp_ops.review_workspace import SLUG, ReviewError

_ALLOWED_OS_ENV = ("SYSTEMROOT", "WINDIR", "TEMP", "TMP")
_FORBIDDEN_PREFIXES = ("WP_SECRET_", "WOO_", "MAIL_", "SMTP_", "OPENROUTER_", "TELEGRAM_", "MESSENGER_")
_STARTUP_TIMEOUT_S = 15.0
_PLUGIN_ENTRY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,60}/[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.php$")
_PLUGIN_MAIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.php$")
_PHP_SYS_VERSION_RE = re.compile(r"PHP (8\.[234]\.\d+)")
_WP_VERSION_RE = re.compile(r"\$wp_version\s*=\s*(['\"])([^'\"]+)\1\s*;")
_PROBE_ENV_KEYS = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")


def runtime_dir_from_env(explicit: Path | None = None) -> Path:
    """Resolve the pinned runtime directory from an explicit arg, env, or default."""
    if explicit is not None:
        return Path(explicit)
    configured = os.environ.get("AZOM_WP_RUNTIME_DIR", "").strip()
    if configured:
        return Path(configured)
    return default_dest()


def build_php_child_env() -> dict[str, str]:
    """Return a minimal synthetic env for the PHP child process. Never copies secrets."""
    env: dict[str, str] = {"AZOM_REVIEW_SYNTHETIC": "1"}
    for key in _ALLOWED_OS_ENV:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    if FORBIDDEN_ENV_NAMES.intersection(env):
        raise ReviewError("SandboxSecretRefused")
    if any(name.upper().startswith(_FORBIDDEN_PREFIXES) for name in env):
        raise ReviewError("SandboxSecretRefused")
    return env


def _pick_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _copy_component_tree(source: Path, dest: Path) -> None:
    """Copy a local theme or plugin directory into the sandbox without following symlinks."""
    source_root = Path(source).resolve()
    if not source_root.is_dir():
        raise ReviewError("WorkspaceMissing")
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    dest_root = dest.resolve()
    for path in sorted(source_root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        rel = path.relative_to(source_root)
        target = (dest_root / rel).resolve()
        if dest_root not in target.parents:
            raise ReviewError("PathRejected")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())


_copy_theme_tree = _copy_component_tree


def _php_serialize_list(items: list[str]) -> str:
    """Serialize a list of strings as a PHP indexed array using UTF-8 byte lengths."""
    if not items:
        return "a:0:{}"
    parts = [f'i:{idx};s:{len(item.encode("utf-8"))}:"{item}";' for idx, item in enumerate(items)]
    return f"a:{len(items)}:{{{''.join(parts)}}}"


def _write_minimal_synthetic_theme(dest: Path) -> None:
    """Write a minimal synthetic host theme for isolated plugin-only runs."""
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "style.css").write_text(
        "/*\nTheme Name: Azom Synthetic Plugin Host\nVersion: 1.0.0\n*/\n",
        encoding="utf-8",
    )
    (dest / "functions.php").write_text(
        "<?php\nadd_action('wp_enqueue_scripts', function () { wp_enqueue_style('azom-host-style', get_stylesheet_uri(), array(), '1.0.0'); });\n",
        encoding="utf-8",
    )
    (dest / "index.php").write_text(
        "<!doctype html>\n"
        "<html <?php language_attributes(); ?>>\n"
        "<head>\n"
        "<meta charset=\"<?php bloginfo('charset'); ?>\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        "<?php wp_head(); ?>\n"
        "</head>\n"
        "<body <?php body_class(); ?>>\n"
        "<?php wp_body_open(); ?>\n"
        '<main data-azom-theme-marker="azom-plugin-host">Azom Plugin Host</main>\n'
        "<?php wp_footer(); ?>\n"
        "</body>\n"
        "</html>\n",
        encoding="utf-8",
    )


def _detect_plugin_main_file(plugin_dir: Path, plugin_slug: str) -> str:
    """Locate the main PHP entry file inside a plugin directory."""
    root = Path(plugin_dir)
    if not root.is_dir():
        raise ReviewError("ComponentMissing")
    preferred = root / f"{plugin_slug}.php"
    if preferred.is_file() and not preferred.is_symlink():
        return preferred.name
    php_files = sorted(
        (p for p in root.iterdir() if p.is_file() and not p.is_symlink() and p.suffix.lower() == ".php"),
        key=lambda p: p.name,
    )
    for path in php_files:
        text = path.read_text(encoding="utf-8", errors="replace")[:8192]
        if "Plugin Name:" in text:
            return path.name
    if php_files:
        return php_files[0].name
    raise ReviewError("ComponentMissing")


def resolve_local_runtime(runtime_dir: Path) -> dict[str, Any]:
    """Resolve the pinned runtime manifest, with non-Windows system PHP fallback."""
    resolved_dir = Path(runtime_dir)
    try:
        return check_runtime(resolved_dir)
    except RuntimeFetchError:
        if os.name == "nt":
            raise ReviewError("SandboxRuntimeUnavailable") from None
    except OSError:
        raise ReviewError("SandboxRuntimeUnavailable") from None

    wp_dir = resolved_dir / "wordpress"
    version_file = wp_dir / "wp-includes" / "version.php"
    php_path = shutil.which("php")
    if not version_file.is_file() or version_file.is_symlink() or not php_path:
        raise ReviewError("SandboxRuntimeUnavailable")

    probe_env = {k: v for k, v in os.environ.items() if k.upper() in _PROBE_ENV_KEYS}
    try:
        ver_proc = subprocess.run(
            [php_path, "-v"],
            shell=False,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=probe_env,
        )
        mod_proc = subprocess.run(
            [php_path, "-m"],
            shell=False,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=probe_env,
        )
        version_text = version_file.read_text(encoding="utf-8", errors="replace")[:65536]
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReviewError("SandboxRuntimeUnavailable") from exc

    if ver_proc.returncode != 0 or mod_proc.returncode != 0:
        raise ReviewError("SandboxRuntimeUnavailable")

    php_match = _PHP_SYS_VERSION_RE.search(f"{ver_proc.stdout or ''}\n{ver_proc.stderr or ''}")
    if php_match is None:
        raise ReviewError("SandboxRuntimeUnavailable")

    modules = {line.strip().lower() for line in (mod_proc.stdout or "").splitlines() if line.strip()}
    if "pdo_sqlite" not in modules:
        raise ReviewError("SandboxRuntimeUnavailable")

    wp_match = _WP_VERSION_RE.search(version_text)
    if wp_match is None:
        raise ReviewError("SandboxRuntimeUnavailable")

    return {
        "php_path": php_path,
        "wordpress_path": str(wp_dir.resolve()),
        "wordpress_version": wp_match.group(2),
        "php_version": php_match.group(1),
        "system_php": True,
    }


def _php_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _init_sqlite_db(
    db_path: Path,
    *,
    base_url: str,
    theme_slug: str,
    parent_slug: str | None = None,
    active_plugins: list[str] | None = None,
    site_title: str = "Azom Synthetic Fixture",
) -> None:
    plugin_entries = list(active_plugins or [])
    for entry in plugin_entries:
        if not isinstance(entry, str) or not _PLUGIN_ENTRY_RE.fullmatch(entry):
            raise ReviewError("PathRejected")
    serialized_plugins = _php_serialize_list(plugin_entries)
    if db_path.exists():
        db_path.unlink()
    template_slug = parent_slug or theme_slug
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(
            """
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
        )
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


def _render_db_dropin() -> str:
    return r"""<?php
if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

class Azom_SQLite_WPDB extends wpdb {
    /** @var PDO|null */
    protected $pdo = null;
    /** @var int */
    protected $found_rows_count = 0;

    public function __construct( $dbuser, $dbpassword, $dbname, $dbhost ) {
        $this->is_mysql = false;
        parent::__construct( $dbuser, $dbpassword, $dbname, $dbhost );
    }

    public function db_connect( $allow_bail = true ) {
        $path = defined( 'AZOM_SQLITE_PATH' ) ? AZOM_SQLITE_PATH : ':memory:';
        try {
            $this->pdo = new PDO( 'sqlite:' . $path );
            $this->pdo->setAttribute( PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION );
            $this->pdo->setAttribute( PDO::ATTR_DEFAULT_FETCH_MODE, PDO::FETCH_OBJ );
            $this->pdo->sqliteCreateFunction(
                'REGEXP',
                static function ( $pattern, $subject ) {
                    if ( $subject === null ) {
                        return 0;
                    }
                    return preg_match( '/' . str_replace( '/', '\/', (string) $pattern ) . '/i', (string) $subject ) ? 1 : 0;
                },
                2
            );
            $this->dbh   = $this->pdo;
            $this->ready = true;
            $this->has_connected = true;
            $this->charset = 'utf8mb4';
            $this->collate = 'utf8mb4_unicode_ci';
            return true;
        } catch ( Throwable $e ) {
            $this->last_error = $e->getMessage();
            $this->ready      = false;
            return false;
        }
    }

    public function check_connection( $allow_bail = true ) {
        return $this->pdo instanceof PDO;
    }

    public function set_charset( $dbh, $charset = null, $collate = null ) {
        $this->charset = $charset ?: 'utf8mb4';
        $this->collate = $collate ?: 'utf8mb4_unicode_ci';
    }

    public function set_sql_mode( $modes = array() ) {
        return;
    }

    public function select( $db, $dbh = null ) {
        return true;
    }

    public function close() {
        $this->pdo = null;
        $this->dbh = null;
        $this->ready = false;
        return true;
    }

    public function db_version() {
        return '8.0.36';
    }

    public function db_server_info() {
        return '8.0.36';
    }

    public function has_cap( $db_cap ) {
        $cap = strtolower( (string) $db_cap );
        return in_array( $cap, array( 'collation', 'group_concat', 'subqueries', 'set_charset', 'utf8mb4', 'utf8mb4_520' ), true );
    }

    public function get_table_charset( $table ) {
        return 'utf8mb4';
    }

    public function get_col_charset( $table, $column ) {
        return 'utf8mb4';
    }

    public function _real_escape( $data ) {
        if ( ! is_scalar( $data ) ) {
            return '';
        }
        return str_replace(
            array( "\\", "\0", "\n", "\r", "'", '"' ),
            array( "\\\\", "\\0", "\\n", "\\r", "\\'", '\\"' ),
            (string) $data
        );
    }

    public function flush() {
        $this->last_result   = array();
        $this->col_info      = null;
        $this->last_query    = null;
        $this->rows_affected = 0;
        $this->num_rows      = 0;
        $this->last_error    = '';
        $this->result        = null;
    }

    protected function translate_sql( string $sql ): array {
        $calc_found = false;
        $trimmed    = trim( $sql );

        if ( preg_match( '/^SELECT\s+FOUND_ROWS\(\s*\)/i', $trimmed ) ) {
            return array( 'SELECT ' . (int) $this->found_rows_count . ' AS found_rows', false, false );
        }

        if ( preg_match( '/^SHOW\s+TABLES\s+LIKE\s+(.+)$/i', $trimmed, $m ) ) {
            return array( "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE " . $m[1], false, false );
        }

        if ( preg_match( '/^SHOW\s+TABLES\s*$/i', $trimmed ) ) {
            return array( "SELECT name FROM sqlite_master WHERE type='table'", false, false );
        }

        if ( preg_match( '/^(?:SHOW\s+(?:FULL\s+)?COLUMNS\s+FROM|DESCRIBE)\s+`?([A-Za-z0-9_]+)`?/i', $trimmed, $m ) ) {
            return array( 'PRAGMA table_info(' . $m[1] . ')', false, false );
        }

        if ( preg_match( '/^(?:SET|LOCK\s+TABLES|UNLOCK\s+TABLES)/i', $trimmed ) ) {
            return array( '', false, true );
        }

        if ( stripos( $trimmed, 'SQL_CALC_FOUND_ROWS' ) !== false ) {
            $calc_found = true;
            $trimmed    = preg_replace( '/\bSQL_CALC_FOUND_ROWS\b/i', '', $trimmed );
        }

        $trimmed = preg_replace( '/\b(?:FORCE|USE|IGNORE)\s+INDEX\s*\([^)]*\)/i', '', $trimmed );
        $trimmed = preg_replace( '/\bINSERT\s+IGNORE\s+INTO\b/i', 'INSERT OR IGNORE INTO', $trimmed );
        $trimmed = preg_replace( '/\bREPLACE\s+INTO\b/i', 'INSERT OR REPLACE INTO', $trimmed );
        $trimmed = preg_replace( '/\bON\s+DUPLICATE\s+KEY\s+UPDATE\b.*$/is', '', $trimmed, -1, $odku );
        if ( $odku > 0 ) {
            $trimmed = preg_replace( '/^INSERT\s+(?:OR\s+IGNORE\s+)?INTO\b/i', 'INSERT OR REPLACE INTO', $trimmed );
        }
        $trimmed = preg_replace( '/\bNOW\(\s*\)/i', "datetime('now')", $trimmed );
        $trimmed = preg_replace( '/\bUTC_TIMESTAMP\(\s*\)/i', "datetime('now')", $trimmed );
        $trimmed = preg_replace( '/\bUNIX_TIMESTAMP\(\s*\)/i', "strftime('%s','now')", $trimmed );
        $trimmed = preg_replace( '/\bBINARY\s+/i', '', $trimmed );
        $trimmed = $this->unescape_mysql_literals( $trimmed );
        return array( $trimmed, $calc_found, false );
    }

    protected function unescape_mysql_literals( string $sql ): string {
        $out = '';
        $len = strlen( $sql );
        $in_single = false;
        for ( $i = 0; $i < $len; $i++ ) {
            $ch = $sql[ $i ];
            if ( ! $in_single ) {
                $out .= $ch;
                if ( $ch === "'" ) {
                    $in_single = true;
                }
                continue;
            }
            if ( $ch === '\\' && $i + 1 < $len ) {
                $next = $sql[ $i + 1 ];
                if ( $next === "'" ) {
                    $out .= "''";
                    $i++;
                    continue;
                }
                if ( $next === '\\' ) {
                    $out .= '\\';
                    $i++;
                    continue;
                }
                if ( $next === '"' ) {
                    $out .= '"';
                    $i++;
                    continue;
                }
                if ( $next === 'n' ) {
                    $out .= "\n";
                    $i++;
                    continue;
                }
                if ( $next === 'r' ) {
                    $out .= "\r";
                    $i++;
                    continue;
                }
                if ( $next === '0' ) {
                    $out .= "\0";
                    $i++;
                    continue;
                }
            }
            if ( $ch === "'" ) {
                if ( $i + 1 < $len && $sql[ $i + 1 ] === "'" ) {
                    $out .= "''";
                    $i++;
                    continue;
                }
                $in_single = false;
            }
            $out .= $ch;
        }
        return $out;
    }

    public function query( $query ) {
        if ( ! $this->ready || ! ( $this->pdo instanceof PDO ) ) {
            $this->check_current_query = true;
            return false;
        }

        $query = apply_filters( 'query', $query );
        $this->flush();
        $this->last_query = $query;
        $this->num_queries++;

        list( $sql, $calc_found, $noop ) = $this->translate_sql( (string) $query );
        if ( $noop || trim( $sql ) === '' ) {
            return 0;
        }

        try {
            $is_select = (bool) preg_match( '/^\s*(?:SELECT|PRAGMA|WITH|EXPLAIN)\b/i', $sql );
            $stmt      = $this->pdo->query( $sql );
            if ( $stmt === false ) {
                return false;
            }
            if ( $is_select ) {
                $rows               = $stmt->fetchAll( PDO::FETCH_OBJ );
                $this->last_result  = is_array( $rows ) ? $rows : array();
                $this->num_rows     = count( $this->last_result );
                if ( $calc_found ) {
                    $this->found_rows_count = $this->num_rows;
                }
                return $this->num_rows;
            }
            $this->rows_affected = $stmt->rowCount();
            if ( preg_match( '/^\s*(?:INSERT|REPLACE)\b/i', $sql ) ) {
                $this->insert_id = (int) $this->pdo->lastInsertId();
            }
            return $this->rows_affected;
        } catch ( Throwable $e ) {
            $this->last_error = $e->getMessage();
            return false;
        }
    }
}

$wpdb = new Azom_SQLite_WPDB( DB_USER, DB_PASSWORD, DB_NAME, DB_HOST );
"""


def _render_egress_guard() -> str:
    return r"""<?php
if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

add_filter(
    'pre_http_request',
    static function ( $preempt, $parsed_args, $url ) {
        $host = strtolower( (string) parse_url( (string) $url, PHP_URL_HOST ) );
        if ( $host === '127.0.0.1' || $host === 'localhost' ) {
            return $preempt;
        }
        if ( defined( 'AZOM_EGRESS_LOG' ) ) {
            $entry = json_encode(
                array(
                    'url'    => (string) $url,
                    'host'   => $host,
                    'source' => 'wordpress_http',
                ),
                JSON_UNESCAPED_SLASHES
            );
            @file_put_contents( AZOM_EGRESS_LOG, $entry . "\n", FILE_APPEND | LOCK_EX );
        }
        return new WP_Error( 'azom_egress_blocked', 'External HTTP blocked in synthetic review sandbox.' );
    },
    1,
    3
);
"""


def _render_wp_config(
    *,
    wp_core: Path,
    wp_content: Path,
    base_url: str,
    sqlite_path: Path,
    debug_log: Path,
    egress_log: Path,
) -> str:
    wp_core_str = _php_escape(str(wp_core.resolve()).replace("\\", "/") + "/")
    wp_content_str = _php_escape(str(wp_content.resolve()).replace("\\", "/"))
    sqlite_str = _php_escape(str(sqlite_path.resolve()).replace("\\", "/"))
    debug_str = _php_escape(str(debug_log.resolve()).replace("\\", "/"))
    egress_str = _php_escape(str(egress_log.resolve()).replace("\\", "/"))
    url_str = _php_escape(base_url.rstrip("/"))
    return f"""<?php
define( 'DB_NAME', 'azom_synthetic' );
define( 'DB_USER', 'synthetic' );
define( 'DB_PASSWORD', '' );
define( 'DB_HOST', '127.0.0.1' );
define( 'DB_CHARSET', 'utf8mb4' );
define( 'DB_COLLATE', '' );
$table_prefix = 'wp_';

define( 'AUTH_KEY',         'azom-synthetic-auth-key-not-production' );
define( 'SECURE_AUTH_KEY',  'azom-synthetic-secure-auth-key-not-production' );
define( 'LOGGED_IN_KEY',    'azom-synthetic-logged-in-key-not-production' );
define( 'NONCE_KEY',        'azom-synthetic-nonce-key-not-production' );
define( 'AUTH_SALT',        'azom-synthetic-auth-salt-not-production' );
define( 'SECURE_AUTH_SALT', 'azom-synthetic-secure-auth-salt-not-production' );
define( 'LOGGED_IN_SALT',   'azom-synthetic-logged-in-salt-not-production' );
define( 'NONCE_SALT',       'azom-synthetic-nonce-salt-not-production' );

define( 'WP_HOME', '{url_str}' );
define( 'WP_SITEURL', '{url_str}' );
define( 'WP_CONTENT_DIR', '{wp_content_str}' );
define( 'WP_CONTENT_URL', '{url_str}/wp-content' );
define( 'AZOM_SQLITE_PATH', '{sqlite_str}' );
define( 'AZOM_EGRESS_LOG', '{egress_str}' );

define( 'WP_HTTP_BLOCK_EXTERNAL', true );
define( 'WP_ACCESSIBLE_HOSTS', '127.0.0.1,localhost' );
define( 'DISABLE_WP_CRON', true );
define( 'AUTOMATIC_UPDATER_DISABLED', true );
define( 'DISALLOW_FILE_EDIT', true );
define( 'DISALLOW_FILE_MODS', true );

define( 'WP_DEBUG', true );
define( 'WP_DEBUG_DISPLAY', false );
define( 'WP_DEBUG_LOG', '{debug_str}' );

if ( ! defined( 'ABSPATH' ) ) {{
    define( 'ABSPATH', '{wp_core_str}' );
}}

require_once ABSPATH . 'wp-settings.php';
"""


def _render_router(*, wp_core: Path, wp_content: Path, wp_config: Path) -> str:
    wp_core_str = _php_escape(str(wp_core.resolve()).replace("\\", "/"))
    wp_content_str = _php_escape(str(wp_content.resolve()).replace("\\", "/"))
    wp_config_str = _php_escape(str(wp_config.resolve()).replace("\\", "/"))
    return f"""<?php
$azom_wp_core    = '{wp_core_str}';
$azom_wp_content = '{wp_content_str}';
$azom_wp_config  = '{wp_config_str}';

$path = (string) parse_url( $_SERVER['REQUEST_URI'] ?? '/', PHP_URL_PATH );
if ( str_contains( $path, '..' ) ) {{
    http_response_code( 400 );
    exit;
}}

$mime = array(
    'css'   => 'text/css; charset=UTF-8',
    'js'    => 'application/javascript; charset=UTF-8',
    'json'  => 'application/json; charset=UTF-8',
    'svg'   => 'image/svg+xml',
    'png'   => 'image/png',
    'jpg'   => 'image/jpeg',
    'jpeg'  => 'image/jpeg',
    'gif'   => 'image/gif',
    'webp'  => 'image/webp',
    'woff2' => 'font/woff2',
    'woff'  => 'font/woff',
    'txt'   => 'text/plain; charset=UTF-8',
    'html'  => 'text/html; charset=UTF-8',
);

if ( str_starts_with( $path, '/wp-content/' ) ) {{
    $rel  = substr( $path, strlen( '/wp-content/' ) );
    $full = realpath( $azom_wp_content . '/' . $rel );
    $base = realpath( $azom_wp_content );
    if ( $full !== false && $base !== false && str_starts_with( $full, $base . DIRECTORY_SEPARATOR ) && is_file( $full ) ) {{
        $ext = strtolower( pathinfo( $full, PATHINFO_EXTENSION ) );
        if ( $ext === 'php' ) {{
            http_response_code( 403 );
            exit;
        }}
        header( 'Content-Type: ' . ( $mime[ $ext ] ?? 'application/octet-stream' ) );
        readfile( $full );
        exit;
    }}
    http_response_code( 404 );
    exit;
}}

if ( $path !== '/' ) {{
    $core_file = realpath( $azom_wp_core . $path );
    $core_base = realpath( $azom_wp_core );
    if ( $core_file !== false && $core_base !== false && str_starts_with( $core_file, $core_base . DIRECTORY_SEPARATOR ) && is_file( $core_file ) ) {{
        $ext = strtolower( pathinfo( $core_file, PATHINFO_EXTENSION ) );
        if ( $ext !== 'php' ) {{
            return false;
        }}
    }}
}}

define( 'WP_USE_THEMES', true );
$_SERVER['SCRIPT_FILENAME'] = $azom_wp_core . '/index.php';
$_SERVER['SCRIPT_NAME']     = '/index.php';
$_SERVER['PHP_SELF']        = '/index.php';

require_once $azom_wp_config;
wp();
require_once ABSPATH . WPINC . '/template-loader.php';
"""


class LocalWordPressSandbox:
    """One opt-in, loopback-only WordPress + PHP process."""

    def __init__(
        self,
        *,
        plan_id: str,
        sandbox_dir: Path,
        theme_slug: str = "azom-plugin-host",
        theme_dir: Path | None = None,
        runtime_dir: Path | None = None,
        parent_theme_slug: str | None = None,
        parent_theme_dir: Path | None = None,
        plugin_slug: str | None = None,
        plugin_dir: Path | None = None,
        plugin_main_file: str | None = None,
        startup_timeout_s: float = _STARTUP_TIMEOUT_S,
    ) -> None:
        if theme_dir is None and plugin_dir is None:
            raise ReviewError("WorkspaceMissing")
        if not SLUG.fullmatch(theme_slug):
            raise ReviewError("SlugRejected")
        if parent_theme_slug is not None and not SLUG.fullmatch(parent_theme_slug):
            raise ReviewError("SlugRejected")
        if plugin_slug is not None and not SLUG.fullmatch(plugin_slug):
            raise ReviewError("SlugRejected")
        if plugin_dir is not None and not plugin_slug:
            raise ReviewError("SlugRejected")
        if plugin_main_file is not None and not _PLUGIN_MAIN_RE.fullmatch(plugin_main_file):
            raise ReviewError("PathRejected")
        self.plan_id = plan_id
        self.sandbox_dir = Path(sandbox_dir).resolve()
        self.theme_slug = theme_slug
        self.theme_dir = Path(theme_dir).resolve() if theme_dir is not None else None
        self.runtime_dir = runtime_dir_from_env(runtime_dir)
        self.parent_theme_slug = parent_theme_slug
        self.parent_theme_dir = Path(parent_theme_dir).resolve() if parent_theme_dir else None
        self.plugin_slug = plugin_slug
        self.plugin_dir = Path(plugin_dir).resolve() if plugin_dir is not None else None
        self.plugin_main_file = plugin_main_file
        self.startup_timeout_s = max(2.0, float(startup_timeout_s))
        self.port: int | None = None
        self.url: str = ""
        self._proc: subprocess.Popen[bytes] | None = None
        self._stdout_handle: Any = None
        self._stderr_handle: Any = None
        self._env_names: list[str] = []
        self._php_error_log = self.sandbox_dir / "php-errors.log"
        self._wp_debug_log = self.sandbox_dir / "wp-debug.log"
        self._egress_log = self.sandbox_dir / "egress-attempts.jsonl"

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> dict[str, Any]:
        if not sandbox_enabled():
            raise ReviewError("SandboxDisabled")
        try:
            manifest = resolve_local_runtime(self.runtime_dir)
        except (RuntimeFetchError, OSError) as exc:
            raise ReviewError("SandboxRuntimeUnavailable") from exc

        php_binary = Path(str(manifest["php_path"])).resolve()
        wp_core = Path(str(manifest["wordpress_path"])).resolve()
        ext_dir = (php_binary.parent / "ext").resolve()
        if not php_binary.is_file() or not wp_core.is_dir():
            raise ReviewError("SandboxRuntimeUnavailable")

        self.sandbox_dir.mkdir(parents=True, exist_ok=True)
        wp_content = self.sandbox_dir / "wp-content"
        themes_dir = wp_content / "themes"
        plugins_dir = wp_content / "plugins"
        mu_plugins = wp_content / "mu-plugins"
        themes_dir.mkdir(parents=True, exist_ok=True)
        plugins_dir.mkdir(parents=True, exist_ok=True)
        mu_plugins.mkdir(parents=True, exist_ok=True)

        if self.theme_dir is not None:
            _copy_component_tree(self.theme_dir, themes_dir / self.theme_slug)
        else:
            _write_minimal_synthetic_theme(themes_dir / self.theme_slug)
        if self.parent_theme_slug:
            if self.parent_theme_dir:
                _copy_component_tree(self.parent_theme_dir, themes_dir / self.parent_theme_slug)
            else:
                _write_minimal_synthetic_theme(themes_dir / self.parent_theme_slug)

        active_plugins: list[str] = []
        if self.plugin_slug and self.plugin_dir:
            _copy_component_tree(self.plugin_dir, plugins_dir / self.plugin_slug)
            main_file = self.plugin_main_file or _detect_plugin_main_file(self.plugin_dir, self.plugin_slug)
            if not _PLUGIN_MAIN_RE.fullmatch(main_file):
                raise ReviewError("PathRejected")
            active_plugins = [f"{self.plugin_slug}/{main_file}"]

        (wp_content / "db.php").write_text(_render_db_dropin(), encoding="utf-8")
        (mu_plugins / "azom-egress-guard.php").write_text(_render_egress_guard(), encoding="utf-8")

        self.port = _pick_loopback_port()
        self.url = f"http://127.0.0.1:{self.port}/"
        sqlite_path = self.sandbox_dir / "wordpress.sqlite3"
        _init_sqlite_db(
            sqlite_path,
            base_url=self.url.rstrip("/"),
            theme_slug=self.theme_slug,
            parent_slug=self.parent_theme_slug,
            active_plugins=active_plugins,
        )

        wp_config = self.sandbox_dir / "wp-config.php"
        wp_config.write_text(
            _render_wp_config(
                wp_core=wp_core,
                wp_content=wp_content,
                base_url=self.url,
                sqlite_path=sqlite_path,
                debug_log=self._wp_debug_log,
                egress_log=self._egress_log,
            ),
            encoding="utf-8",
        )
        router = self.sandbox_dir / "router.php"
        router.write_text(
            _render_router(wp_core=wp_core, wp_content=wp_content, wp_config=wp_config),
            encoding="utf-8",
        )

        env = build_php_child_env()
        self._env_names = sorted(env)
        if manifest.get("system_php") is True:
            command = [
                str(php_binary),
                "-d",
                "display_errors=0",
                "-d",
                "log_errors=1",
                "-d",
                f"error_log={self._php_error_log}",
                "-S",
                f"127.0.0.1:{self.port}",
                "-t",
                str(wp_core),
                str(router),
            ]
        else:
            command = [
                str(php_binary),
                "-n",
                "-d",
                f"extension_dir={ext_dir}",
                "-d",
                "extension=pdo_sqlite",
                "-d",
                "extension=sqlite3",
                "-d",
                "extension=mbstring",
                "-d",
                "extension=fileinfo",
                "-d",
                "display_errors=0",
                "-d",
                "log_errors=1",
                "-d",
                f"error_log={self._php_error_log}",
                "-S",
                f"127.0.0.1:{self.port}",
                "-t",
                str(wp_core),
                str(router),
            ]
        stdout_path = self.sandbox_dir / "php-server.out.log"
        stderr_path = self.sandbox_dir / "php-server.err.log"
        self._stdout_handle = stdout_path.open("wb")
        self._stderr_handle = stderr_path.open("wb")
        try:
            self._proc = subprocess.Popen(
                command,
                shell=False,
                stdout=self._stdout_handle,
                stderr=self._stderr_handle,
                cwd=str(self.sandbox_dir),
                env=env,
            )
            self._wait_ready()
        except Exception as exc:
            self.stop()
            if isinstance(exc, ReviewError):
                raise
            raise ReviewError("SandboxRuntimeUnavailable") from exc
        return self.record()

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + self.startup_timeout_s
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        while time.monotonic() < deadline:
            if not self.is_alive():
                raise ReviewError("SandboxRuntimeUnavailable")
            try:
                with opener.open(self.url, timeout=2.0) as resp:
                    if resp.status >= 100:
                        return
            except urllib.error.HTTPError as exc:
                # An HTTP 500 from a broken negative theme still proves PHP is up and serving.
                if exc.code >= 100:
                    return
            except (urllib.error.URLError, OSError):
                time.sleep(0.15)
        raise ReviewError("SandboxRuntimeUnavailable")

    def record(self) -> dict[str, Any]:
        alive = self.is_alive()
        pid = self._proc.pid if self._proc is not None and alive else None
        return {
            "engine": "local_php_wp" if alive else "unavailable",
            "database": "synthetic_sqlite" if alive else "not_created",
            "network": "loopback_only" if alive else "not_run",
            "env_names": list(self._env_names),
            "state": "running" if alive else "stopped",
            "process_alive": alive,
            "bind_host": "127.0.0.1",
            "port": None,
            "published_port": False,
            "docker_socket": False,
            "broad_host_mount": False,
            "payments_enabled": False,
            "checkout_enabled": False,
            "admin_password_stored": False,
            "pid": pid,
            "plan_id": self.plan_id,
        }

    def read_php_errors(self) -> list[str]:
        lines: list[str] = []
        for path in (self._php_error_log, self._wp_debug_log):
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for raw in text.splitlines():
                cleaned = raw.strip()
                if cleaned:
                    lines.append(cleaned[:400])
        return lines

    def read_egress_attempts(self) -> list[dict[str, str]]:
        if not self._egress_log.is_file():
            return []
        attempts: list[dict[str, str]] = []
        for raw in self._egress_log.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                item = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                attempts.append(
                    {
                        "url": str(item.get("url") or "")[:300],
                        "host": str(item.get("host") or "")[:120],
                        "source": str(item.get("source") or "wordpress_http")[:60],
                    }
                )
        return attempts

    def stop(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5.0)
        for attr in ("_stdout_handle", "_stderr_handle"):
            handle = getattr(self, attr, None)
            setattr(self, attr, None)
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass

    def __enter__(self) -> LocalWordPressSandbox:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


def make_local_sandbox_gate(sandbox: LocalWordPressSandbox) -> SandboxGate:
    """Bind one LocalWordPressSandbox to SandboxGate for opt-in verification."""

    def _starter(site: object, plan_id: str, *, high_risk: bool) -> dict[str, Any]:
        del site, high_risk
        if plan_id != sandbox.plan_id:
            raise ReviewError("SandboxRuntimeUnavailable")
        return sandbox.start()

    def _stopper(_record: dict[str, Any]) -> None:
        sandbox.stop()

    return SandboxGate(runtime_starter=_starter, runtime_stopper=_stopper)
