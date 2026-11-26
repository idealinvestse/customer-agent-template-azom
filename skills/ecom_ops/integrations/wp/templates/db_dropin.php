<?php
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
