"""Composable, zero-secret-leakage systemcheck and healthcheck for AzomOps.

Provides two read-only diagnostic entrypoints:
- ``run_systemcheck(...)``: host OS/Python/packages, Linux system binaries,
  disk/RAM resources, YAML configs, ``.env`` POSIX 0600 permissions,
  ``AZOM_DATA_DIR`` writability, SQLite integrity & WAL checks, production
  safety & live fail-closed posture, opt-in DNS/TLS/port network checks, and
  Linux systemd unit enablement checks.
- ``run_healthcheck(...)``: local dashboard HTTP probes (``/live``, ``/health``,
  ``/ready``), opt-in public HTTPS + TLS + ``/metrics`` lock probe, Linux
  systemd active-state checks, and ``collect_runtime_snapshot()`` integration.

Exit code convention (via ``report.exit_code(strict=...)``):
- ``0``: OK (no failures; warnings allowed unless ``strict=True``)
- ``1``: Failure (one or more ``fail`` checks)
- ``2``: Warning with ``--strict`` (no ``fail`` checks, at least one ``warn``)
"""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import shutil
import socket
import sqlite3
import ssl
import stat
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from ecom_ops import __version__
from ecom_ops.bot.actors import (
    channel_posture,
    effective_telegram_actor_map,
    effective_telegram_allowlist,
)
from ecom_ops.budget import budget_status
from ecom_ops.model_router import circuit_breaker, get_model_router, reset_model_router
from ecom_ops.runtime_env import _parse_env_file, bootstrap_runtime, env_flag
from ecom_ops.runtime_health import collect_runtime_snapshot
from ecom_ops.security import redact_secrets

REQUIRED_PACKAGES: tuple[tuple[str, str], ...] = (
    ("yaml", "PyYAML"),
    ("requests", "requests"),
    ("flask", "Flask"),
)
PRODUCTION_PACKAGES: tuple[tuple[str, str], ...] = (
    ("waitress", "waitress"),
    ("paramiko", "paramiko"),
)
CORE_CONFIG_FILES: tuple[str, ...] = (
    "sites.yaml",
    "prompts.yaml",
    "model_profiles.yaml",
    "limits.yaml",
)
SQLITE_DBS: tuple[str, ...] = (
    "cases.db",
    "telegram_jobs.sqlite3",
    "messenger_jobs.sqlite3",
    "seo_studio.sqlite3",
    "shop_intel.sqlite3",
    "research_jobs.sqlite3",
)
LINUX_CORE_BINARIES: tuple[str, ...] = ("git", "rsync", "sqlite3", "curl", "openssl")
LINUX_SERVICE_BINARIES: tuple[str, ...] = ("systemctl", "ufw")
AZOM_SERVICES: tuple[str, ...] = ("azom-dashboard.service", "azom-bot.service")
AZOM_TIMERS: tuple[str, ...] = (
    "azom-daily-brief.timer",
    "azom-cases-poll.timer",
    "azom-backup.timer",
    "azom-retention-purge.timer",
    "azom-shop-intel.timer",
)
PROXY_SERVICES: tuple[str, ...] = ("caddy.service", "nginx.service")

DISK_WARN_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
DISK_FAIL_BYTES = 500 * 1024 * 1024  # 500 MiB
RAM_WARN_KB = int(1.5 * 1024 * 1024)  # 1.5 GiB
TLS_WARN_DAYS = 14

_STATUS_BADGE = {
    "ok": "[OK]",
    "warn": "[WARN]",
    "fail": "[FEL]",
    "info": "[INFO]",
    "skip": "[SKIP]",
}


@dataclass(frozen=True)
class CheckItem:
    category: str
    name: str
    status: str  # ok | warn | fail | info | skip
    detail: str
    fix: str = ""


@dataclass
class CheckReport:
    kind: str  # systemcheck | healthcheck
    version: str
    mode: str  # mock | live
    root: str
    data_dir: str
    config_dir: str
    os_name: str = ""
    preferred_route: str = ""
    checks: list[CheckItem] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        if self.blockers:
            return "fail"
        if self.warnings:
            return "warn"
        return "ok"

    def exit_code(self, *, strict: bool = False) -> int:
        if self.blockers:
            return 1
        if strict and self.warnings:
            return 2
        return 0

    def summary_counts(self) -> dict[str, int]:
        counts = {"ok": 0, "warn": 0, "fail": 0, "info": 0, "skip": 0}
        for item in self.checks:
            if item.status in counts:
                counts[item.status] += 1
        counts["total"] = len(self.checks)
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": len(self.blockers) == 0,
            "ready": len(self.blockers) == 0,
            "status": self.status,
            "kind": self.kind,
            "version": self.version,
            "mode": self.mode,
            "os": self.os_name,
            "root": self.root,
            "data_dir": self.data_dir,
            "config_dir": self.config_dir,
            "preferred_route": self.preferred_route,
            "summary": self.summary_counts(),
            "checks": [asdict(c) for c in self.checks],
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "next_steps": list(self.next_steps),
            "details": dict(self.details),
        }


def default_repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _safe_text(raw: Any, *, max_len: int = 220) -> str:
    redacted = redact_secrets(str(raw or ""))
    text = str(redacted).replace("\r", " ").replace("\n", " ").strip()
    return text[:max_len]


def _has_env(name: str) -> bool:
    return bool((os.environ.get(name) or "").strip())


def _is_linux() -> bool:
    return platform.system().lower() == "linux"


def _venv_python(root: Path) -> Path | None:
    for rel in (
        Path(".venv") / "Scripts" / "python.exe",
        Path(".venv") / "bin" / "python",
    ):
        cand = root / rel
        if cand.is_file():
            return cand
    return None


def _resolve_python(root: Path, python_exe: str | None = None) -> str:
    if python_exe:
        return python_exe
    venv_py = _venv_python(root)
    if venv_py is not None:
        return str(venv_py)
    return sys.executable


def _apply_dotenv_defaults(root: Path, *, skip_keys: set[str] | None = None) -> None:
    env_file = root / ".env"
    if not env_file.is_file():
        return
    blocked = skip_keys or set()
    for k, v in _parse_env_file(env_file).items():
        if k in blocked:
            continue
        if not (os.environ.get(k) or "").strip() and v:
            os.environ[k] = v


def resolve_runtime_dirs(root: Path) -> tuple[Path, Path]:
    cfg_raw = (os.environ.get("AZOM_CONFIG_DIR") or "").strip()
    data_raw = (os.environ.get("AZOM_DATA_DIR") or "").strip()
    cfg_dir = Path(cfg_raw) if cfg_raw else root / "config"
    if data_raw:
        data_dir = Path(data_raw)
    elif root.as_posix() == "/opt/azom-agent" and Path("/var/lib/azom").is_dir():
        data_dir = Path("/var/lib/azom")
    else:
        data_dir = root / ".azom-data"
    return cfg_dir, data_dir


def _dir_writable(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path), prefix=".hc_probe_", suffix=".tmp")
        os.close(fd)
        os.unlink(tmp_name)
        return True, str(path)
    except OSError as exc:
        return False, f"ej skrivbar ({type(exc).__name__})"


def _missing_packages(python_exe: str, packages: tuple[tuple[str, str], ...]) -> list[str]:
    if Path(python_exe).resolve() == Path(sys.executable).resolve():
        return [pkg for mod, pkg in packages if importlib.util.find_spec(mod) is None]
    mods = [mod for mod, _ in packages]
    code = (
        "import importlib.util,json,sys;"
        "mods=sys.argv[1:];"
        "print(json.dumps([m for m in mods if importlib.util.find_spec(m) is None]))"
    )
    try:
        out = subprocess.check_output([python_exe, "-c", code, *mods], text=True, timeout=10)
        missing_mods = set(json.loads(out))
        return [pkg for mod, pkg in packages if mod in missing_mods]
    except Exception:
        return [pkg for mod, pkg in packages if importlib.util.find_spec(mod) is None]


def _read_os_release(path: Path = Path("/etc/os-release")) -> dict[str, str]:
    if not path.is_file():
        return {}
    info: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            info[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        return {}
    return info


def _find_binary(name: str) -> str | None:
    cur_path = os.environ.get("PATH", "")
    if os.name == "posix":
        search_path = f"{cur_path}:/usr/sbin:/sbin:/usr/local/bin"
    else:
        search_path = cur_path
    return shutil.which(name, path=search_path)


def _disk_usage_for(path: Path) -> tuple[int, int] | None:
    cand = path
    while not cand.exists():
        if cand.parent == cand:
            break
        cand = cand.parent
    target = cand if cand.exists() else Path("/")
    try:
        usage = shutil.disk_usage(target)
        return int(usage.free), int(usage.total)
    except OSError:
        return None


def _read_meminfo(path: Path = Path("/proc/meminfo")) -> dict[str, int] | None:
    if not path.is_file():
        return None
    parsed: dict[str, int] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if ":" not in line:
                continue
            key, _, rest = line.partition(":")
            parts = rest.strip().split()
            if not parts:
                continue
            try:
                parsed[key.strip()] = int(parts[0])
            except ValueError:
                continue
    except OSError:
        return None
    total_kb = parsed.get("MemTotal")
    if total_kb is None:
        return None
    avail_kb = parsed.get("MemAvailable")
    if avail_kb is None:
        avail_kb = (
            parsed.get("MemFree", 0)
            + parsed.get("Buffers", 0)
            + parsed.get("Cached", 0)
        )
    return {"mem_total_kb": total_kb, "mem_available_kb": avail_kb}


def _env_posix_mode(env_path: Path) -> int | None:
    if not _is_linux() and os.name != "posix":
        return None
    try:
        return stat.S_IMODE(env_path.stat().st_mode)
    except OSError:
        return None


def _check_sqlite_databases(data_dir: Path) -> tuple[CheckItem, dict[str, Any]]:
    checked: dict[str, dict[str, Any]] = {}
    corrupt: list[str] = []
    non_wal: list[str] = []
    missing: list[str] = []

    for db_name in SQLITE_DBS:
        db_path = data_dir / db_name
        if not db_path.is_file() or db_path.is_symlink():
            missing.append(db_name)
            continue
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(
                f"{db_path.resolve().as_uri()}?mode=ro",
                uri=True,
                timeout=2.0,
            )
            qc_row = conn.execute("PRAGMA quick_check(1)").fetchone()
            qc_val = str(qc_row[0]).lower() if qc_row else "error"
            jm_row = conn.execute("PRAGMA journal_mode").fetchone()
            jm_val = str(jm_row[0]).lower() if jm_row else "unknown"
            integrity_ok = qc_val == "ok"
            checked[db_name] = {
                "integrity": qc_val,
                "journal_mode": jm_val,
                "ok": integrity_ok,
            }
            if not integrity_ok:
                corrupt.append(f"{db_name}({qc_val})")
            elif jm_val != "wal":
                non_wal.append(f"{db_name}({jm_val})")
        except sqlite3.Error as exc:
            checked[db_name] = {"integrity": "error", "journal_mode": "unknown", "ok": False}
            corrupt.append(f"{db_name}({type(exc).__name__})")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    meta = {"checked": checked, "missing": missing, "non_wal": non_wal, "corrupt": corrupt}
    if corrupt:
        return (
            CheckItem(
                category="config",
                name="SQLite-databaser",
                status="fail",
                detail=f"integritetsfel: {', '.join(corrupt)}",
                fix="Återställ skadad SQLite-databas från backup innan start.",
            ),
            meta,
        )
    if not checked:
        return (
            CheckItem(
                category="config",
                name="SQLite-databaser",
                status="info",
                detail="inga SQLite-databaser skapade ännu",
            ),
            meta,
        )
    if non_wal:
        return (
            CheckItem(
                category="config",
                name="SQLite-databaser",
                status="warn",
                detail=(
                    f"{len(checked)} OK, ej WAL: {', '.join(non_wal)}"
                ),
                fix="Aktivera WAL-läge på berörd SQLite-databas för säker drift/backup.",
            ),
            meta,
        )
    return (
        CheckItem(
            category="config",
            name="SQLite-databaser",
            status="ok",
            detail=f"{len(checked)}/{len(SQLITE_DBS)} kontrollerade (quick_check=ok, WAL=aktiv)",
        ),
        meta,
    )


def _query_systemd_units(units: tuple[str, ...]) -> dict[str, dict[str, str]]:
    if not _is_linux() or not _find_binary("systemctl"):
        return {}
    cmd = [
        "systemctl",
        "show",
        *units,
        "--property=Id,LoadState,UnitFileState,ActiveState,SubState",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=8, check=False)
    except Exception:
        return {}
    out: dict[str, dict[str, str]] = {}
    blocks = [b.strip() for b in proc.stdout.strip().split("\n\n") if b.strip()]
    for idx, block in enumerate(blocks):
        parsed: dict[str, str] = {}
        for line in block.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                parsed[k.strip()] = v.strip()
        unit_id = parsed.get("Id") or (units[idx] if idx < len(units) else "")
        if unit_id:
            out[unit_id] = {
                "load_state": parsed.get("LoadState", "unknown"),
                "unit_file_state": parsed.get("UnitFileState", "unknown"),
                "active_state": parsed.get("ActiveState", "unknown"),
                "sub_state": parsed.get("SubState", "unknown"),
            }
    return out


def _resolve_dns_a(domain: str) -> list[str]:
    infos = socket.getaddrinfo(domain, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
    ips: list[str] = []
    for _fam, _kind, _proto, _canon, sockaddr in infos:
        ip = str(sockaddr[0])
        if ip not in ips:
            ips.append(ip)
    return ips


def _probe_tls_cert(host: str, port: int = 443, timeout: float = 4.0) -> dict[str, Any]:
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert() or {}
    except Exception as exc:
        return {"ok": False, "error": _safe_text(f"{type(exc).__name__}: {exc}")}

    not_after = str(cert.get("notAfter") or "")
    days_remaining: int | None = None
    if not_after:
        try:
            exp_ts = ssl.cert_time_to_seconds(not_after)
            days_remaining = int((exp_ts - datetime.now(UTC).timestamp()) // 86400)
        except Exception:
            days_remaining = None
    subject = dict(x[0] for x in cert.get("subject", ()) if x)
    issuer = dict(x[0] for x in cert.get("issuer", ()) if x)
    return {
        "ok": days_remaining is None or days_remaining >= 0,
        "days_remaining": days_remaining,
        "not_after": not_after,
        "subject_cn": str(subject.get("commonName") or ""),
        "issuer": str(issuer.get("organizationName") or issuer.get("commonName") or ""),
    }


def _inspect_port_bind(port: int) -> dict[str, Any]:
    ss_bin = _find_binary("ss") if _is_linux() else None
    if ss_bin:
        try:
            proc = subprocess.run([ss_bin, "-tln"], capture_output=True, text=True, timeout=5, check=False)
            if proc.returncode == 0:
                lines = proc.stdout.splitlines()
                loopback = False
                public = False
                for line in lines:
                    parts = line.split()
                    if len(parts) < 4:
                        continue
                    local_addr = parts[3]
                    if not local_addr.endswith(f":{port}"):
                        continue
                    if local_addr.startswith(("127.0.0.1:", "[::1]:", "::1:")):
                        loopback = True
                    else:
                        public = True
                return {
                    "listening": loopback or public,
                    "loopback_bind": loopback,
                    "public_bind": public,
                    "detail": (
                        f"offentlig bind på :{port}"
                        if public
                        else (f"127.0.0.1:{port} lyssnar" if loopback else f"port {port} ej bunden")
                    ),
                }
        except Exception:
            pass

    # Fallback TCP probe on loopback
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return {
                "listening": True,
                "loopback_bind": True,
                "public_bind": False,
                "detail": f"127.0.0.1:{port} svarar",
            }
    except OSError:
        return {
            "listening": False,
            "loopback_bind": False,
            "public_bind": False,
            "detail": f"port {port} ej bunden",
        }


def _http_get(url: str, *, timeout: float = 4.0) -> tuple[int, dict[str, Any] | None, str | None]:
    import requests

    try:
        resp = requests.get(url, timeout=timeout, allow_redirects=False)
        payload: dict[str, Any] | None = None
        try:
            data = resp.json()
            if isinstance(data, dict):
                payload = data
        except Exception:
            payload = None
        return int(resp.status_code), payload, None
    except Exception as exc:
        return 0, None, _safe_text(f"{type(exc).__name__}: {exc}")


def _add_model_and_channel_checks(
    add_fn: Any,
    *,
    is_mock: bool,
    strict_messenger: bool,
) -> str:
    """Append model router, circuit breaker, dashboard, channel, and mail/woo checks."""
    reset_model_router()
    router = get_model_router()
    routes = router.routes()
    preferred = router.preferred_route_id()

    cg = routes.get("chatgpt_oauth") or {}
    cg_ready = bool(cg.get("connected")) and not bool(cg.get("needs_reauth"))
    cg_plan = "plan=ja" if cg.get("plan_usage") else "plan=nej"
    if cg.get("needs_reauth"):
        cg_status, cg_fix = "warn", "Återanslut ChatGPT OAuth via /oscar eller Telegram /oauth chatgpt."
    elif cg_ready:
        cg_status, cg_fix = "ok", ""
    else:
        cg_status, cg_fix = "info", ""
    add_fn(
        CheckItem(
            category="models",
            name="ChatGPT Plus OAuth",
            status=cg_status,
            detail=(
                f"ansluten={'ja' if cg.get('connected') else 'nej'}, {cg_plan}, "
                f"needs_reauth={'ja' if cg.get('needs_reauth') else 'nej'}, "
                f"modell={cg.get('model') or 'gpt-5.2'}"
            ),
            fix=cg_fix,
        )
    )

    xg = routes.get("xai_grok_build") or {}
    grok_bin = (os.environ.get("XAI_GROK_BIN") or "").strip() or (shutil.which("grok") or "")
    add_fn(
        CheckItem(
            category="models",
            name="xAI Grok Build",
            status="ok" if xg.get("configured") else "info",
            detail=(
                f"aktiv={'ja' if xg.get('configured') else 'nej'}, "
                f"grok_cli={'hittad' if grok_bin else 'saknas'}, "
                f"modell={xg.get('model') or 'grok-4'}"
            ),
        )
    )

    or_set = _has_env("OPENROUTER_API_KEY")
    xai_key_set = _has_env("XAI_API_KEY")
    bstat = budget_status()
    add_fn(
        CheckItem(
            category="models",
            name="API-nyckelroutes",
            status="ok" if or_set else "info",
            detail=(
                f"OpenRouter={'satt' if or_set else 'ej satt'} "
                f"(${bstat['used_usd']:.2f}/${bstat['cap_usd']:g}), "
                f"xAI_API_KEY={'satt (opt-in)' if xai_key_set else 'ej satt'}"
            ),
        )
    )

    if not preferred:
        add_fn(
            CheckItem(
                category="models",
                name="Aktiv modellrutt",
                status="warn",
                detail="ingen anropbar LLM-rutt ännu (mall-fallback aktiv)",
                fix="Anslut ChatGPT via /oscar (/oauth chatgpt), xAI eller sätt OPENROUTER_API_KEY.",
            )
        )
    else:
        add_fn(
            CheckItem(
                category="models",
                name="Aktiv modellrutt",
                status="ok",
                detail=f"{preferred} (ordning: {', '.join(router.ordered_route_ids())})",
            )
        )

    breaker_open = {
        k: v
        for k, v in circuit_breaker.status_snapshot().items()
        if str(v.get("state") or "closed") != "closed"
    }
    add_fn(
        CheckItem(
            category="models",
            name="Circuit breaker",
            status="warn" if breaker_open else "ok",
            detail="alla stängda" if not breaker_open else f"öppna: {', '.join(sorted(breaker_open))}",
            fix="Återanslut berörd OAuth eller nollställ circuit breaker i /oscar." if breaker_open else "",
        )
    )

    # Telegram
    tg_token = _has_env("TELEGRAM_BOT_TOKEN")
    tg_allow = bool(effective_telegram_allowlist())
    tg_map = bool(effective_telegram_actor_map())
    if tg_token and not is_mock and (not tg_allow or not tg_map):
        tg_status = "fail"
        tg_fix = (
            "Lägg till Telegram User ID i /oscar/telegram eller sätt "
            "TELEGRAM_ALLOWED_CHAT_IDS och TELEGRAM_ACTOR_MAP i live-läge (fail-closed)."
        )
    elif tg_token:
        tg_status, tg_fix = "ok", ""
    else:
        tg_status = "info"
        tg_fix = "Sätt TELEGRAM_BOT_TOKEN i .env om Telegram-botten ska startas."
    add_fn(
        CheckItem(
            category="channels",
            name="Telegram-bot",
            status=tg_status,
            detail=(
                f"token={'satt' if tg_token else 'saknas (dry-run)'}, "
                f"allowlist={'satt' if tg_allow else 'tom'}, "
                f"actor_map={'satt' if tg_map else 'tom'}"
            ),
            fix=tg_fix,
        )
    )

    # Messenger
    msg_token = _has_env("MESSENGER_PAGE_ACCESS_TOKEN")
    msg_sec = _has_env("MESSENGER_APP_SECRET")
    msg_ver = _has_env("MESSENGER_VERIFY_TOKEN")
    ms_posture = channel_posture("messenger")
    msg_allow = bool(ms_posture.get("allowlist_set"))
    msg_map = bool(ms_posture.get("actor_map_set"))
    msg_any = msg_token or msg_sec or msg_ver
    msg_all = msg_token and msg_sec and msg_ver
    if msg_any and not is_mock and (not msg_all or not msg_allow or not msg_map):
        msg_status = "fail" if strict_messenger else "warn"
        msg_fix = (
            "Komplettera MESSENGER_PAGE_ACCESS_TOKEN, MESSENGER_APP_SECRET, "
            "MESSENGER_VERIFY_TOKEN, MESSENGER_ALLOWED_PSIDS och MESSENGER_ACTOR_MAP."
        )
    elif msg_all:
        msg_status, msg_fix = "ok", ""
    else:
        msg_status, msg_fix = "info", ""
    add_fn(
        CheckItem(
            category="channels",
            name="Meta Messenger",
            status=msg_status,
            detail=(
                f"trio={'komplett' if msg_all else ('delvis' if msg_any else 'ej satt')}, "
                f"allowlist={'satt' if msg_allow else 'tom'}, "
                f"actor_map={'satt' if msg_map else 'tom'}"
            ),
            fix=msg_fix,
        )
    )

    # Mail & Woo
    mail_prov = (os.environ.get("MAIL_PROVIDER") or "gmail").strip().lower()
    woo_url_set = _has_env("WOO_SE_BASE_URL") or _has_env("WOO_BASE_URL")
    woo_keys_set = _has_env("WOO_CONSUMER_KEY") and _has_env("WOO_CONSUMER_SECRET")
    if is_mock:
        woo_status, woo_fix = "ok", ""
    elif woo_url_set and woo_keys_set:
        woo_status, woo_fix = "ok", ""
    else:
        woo_status = "warn"
        woo_fix = "Sätt WOO_BASE_URL (eller WOO_SE_BASE_URL), WOO_CONSUMER_KEY och WOO_CONSUMER_SECRET i .env."
    add_fn(
        CheckItem(
            category="integrations",
            name="Mail & WooCommerce",
            status=woo_status,
            detail=(
                f"mail={mail_prov}, "
                f"woo_url={'satt' if woo_url_set else ('mock' if is_mock else 'saknas')}, "
                f"woo_nycklar={'satt' if woo_keys_set else ('mock' if is_mock else 'saknas')}"
            ),
            fix=woo_fix,
        )
    )
    return preferred


def run_systemcheck(
    root: Path | None = None,
    *,
    mode: str = "auto",
    domain: str | None = None,
    expected_ip: str | None = None,
    check_network: bool = False,
    check_ports: bool = True,
    strict: bool = False,
    apply_dotenv: bool = True,
) -> CheckReport:
    """Run deep system & live-readiness check (offline by default)."""
    del strict  # applied when computing exit_code()
    repo_root = (root or default_repo_root()).resolve()
    os.environ.setdefault("AZOM_CONFIG_DIR", str(repo_root / "config"))
    skip_env = {"AZOM_USE_MOCK"} if mode in {"mock", "live"} else set()
    if apply_dotenv:
        _apply_dotenv_defaults(repo_root, skip_keys=skip_env)
    bootstrap_runtime()

    if mode == "mock":
        os.environ["AZOM_USE_MOCK"] = "1"
    elif mode == "live":
        os.environ["AZOM_USE_MOCK"] = "0"

    is_mock = env_flag("AZOM_USE_MOCK", default=True) if mode == "auto" else (mode == "mock")
    mode_label = "mock" if is_mock else "live"
    cfg_dir, data_dir = resolve_runtime_dirs(repo_root)
    os.environ.setdefault("AZOM_DATA_DIR", str(data_dir))

    checks: list[CheckItem] = []
    steps: list[str] = []
    details: dict[str, Any] = {}

    def _add(item: CheckItem) -> None:
        checks.append(item)
        if item.fix and item.status in {"fail", "warn"} and item.fix not in steps:
            steps.append(item.fix)

    # 1. OS & Python & Packages
    os_rel = _read_os_release() if _is_linux() else {}
    os_label = (
        f"Linux ({os_rel.get('ID', 'unknown')} {os_rel.get('VERSION_ID', '')})".strip()
        if _is_linux()
        else f"{platform.system()} {platform.release()}"
    )
    _add(CheckItem(category="runtime", name="Operativsystem", status="ok", detail=os_label))

    py_ok = sys.version_info >= (3, 11)
    _add(
        CheckItem(
            category="runtime",
            name="Python",
            status="ok" if py_ok else "fail",
            detail=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            fix="Installera Python 3.11+." if not py_ok else "",
        )
    )

    venv_py = _venv_python(repo_root)
    target_py = _resolve_python(repo_root)
    _add(
        CheckItem(
            category="runtime",
            name="Virtuell miljö (.venv)",
            status="ok" if venv_py is not None else ("info" if is_mock else "warn"),
            detail=str(venv_py) if venv_py is not None else f"saknas (använder {sys.executable})",
            fix="Skapa .venv: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" if venv_py is None else "",
        )
    )

    missing_core = _missing_packages(target_py, REQUIRED_PACKAGES)
    _add(
        CheckItem(
            category="runtime",
            name="Python-paket (kärna)",
            status="ok" if not missing_core else "fail",
            detail="PyYAML, requests, Flask installerade" if not missing_core else f"saknas: {', '.join(missing_core)}",
            fix=f"{target_py} -m pip install {' '.join(missing_core)}" if missing_core else "",
        )
    )

    missing_prod = _missing_packages(target_py, PRODUCTION_PACKAGES)
    _add(
        CheckItem(
            category="runtime",
            name="Produktionspaket (WSGI/SSH)",
            status="ok" if not missing_prod else "warn",
            detail="waitress, paramiko installerade" if not missing_prod else f"saknas: {', '.join(missing_prod)}",
            fix=f"{target_py} -m pip install {' '.join(missing_prod)}" if missing_prod else "",
        )
    )

    # 2. Linux system binaries
    if _is_linux():
        missing_bins = [b for b in (*LINUX_CORE_BINARIES, *LINUX_SERVICE_BINARIES) if not _find_binary(b)]
        has_caddy = _find_binary("caddy") is not None
        has_nginx = _find_binary("nginx") is not None
        proxy_found = [p for p, ok in (("caddy", has_caddy), ("nginx", has_nginx)) if ok]
        if missing_bins:
            _add(
                CheckItem(
                    category="system",
                    name="Linux-systembinärer",
                    status="warn",
                    detail=f"saknas: {', '.join(missing_bins)}",
                    fix=f"Installera saknade paket via apt: {' '.join(missing_bins)}",
                )
            )
        else:
            _add(
                CheckItem(
                    category="system",
                    name="Linux-systembinärer",
                    status="ok",
                    detail="git, rsync, sqlite3, curl, openssl, systemctl, ufw hittade",
                )
            )
        _add(
            CheckItem(
                category="system",
                name="Reverse proxy-binär",
                status="ok" if proxy_found else ("info" if is_mock else "warn"),
                detail=f"hittad: {', '.join(proxy_found)}" if proxy_found else "varken caddy eller nginx hittad",
                fix="Installera caddy eller nginx för publik HTTPS." if not proxy_found else "",
            )
        )
    else:
        _add(
            CheckItem(
                category="system",
                name="Linux-systembinärer",
                status="info",
                detail=f"full binärkontroll hoppad på {platform.system()}",
            )
        )

    # 3. Host resources (disk & RAM)
    disk = _disk_usage_for(data_dir)
    if disk is not None:
        free_b, total_b = disk
        free_gb = free_b / (1024**3)
        total_gb = total_b / (1024**3)
        if free_b < DISK_FAIL_BYTES:
            d_status, d_fix = "fail", "Frigör diskutrymme (under 500 MB ledigt)."
        elif free_b < DISK_WARN_BYTES:
            d_status, d_fix = "warn", "Lågt diskutrymme (< 2 GB ledigt); kör ops gc eller rensa loggar."
        else:
            d_status, d_fix = "ok", ""
        details["disk"] = {"free_bytes": free_b, "total_bytes": total_b, "free_gb": round(free_gb, 2)}
        _add(
            CheckItem(
                category="resources",
                name="Diskutrymme",
                status=d_status,
                detail=f"{free_gb:.2f} GB ledigt av {total_gb:.2f} GB",
                fix=d_fix,
            )
        )

    mem = _read_meminfo() if _is_linux() else None
    if mem is not None:
        total_kb = mem["mem_total_kb"]
        avail_kb = mem["mem_available_kb"]
        total_gb_ram = total_kb / (1024 * 1024)
        avail_mb_ram = avail_kb / 1024
        ram_low = total_kb < RAM_WARN_KB
        details["memory"] = {"total_kb": total_kb, "available_kb": avail_kb}
        _add(
            CheckItem(
                category="resources",
                name="RAM-minne",
                status="warn" if ram_low else "ok",
                detail=f"totalt {total_gb_ram:.2f} GB, tillgängligt {avail_mb_ram:.0f} MB",
                fix="Rekommenderat minst 1.5–4 GB RAM (Hetzner CX22/CPX21)." if ram_low else "",
            )
        )
    else:
        _add(
            CheckItem(
                category="resources",
                name="RAM-minne",
                status="info",
                detail="RAM-kontroll via /proc/meminfo endast på Linux",
            )
        )

    # 4. Config YAMLs, .env & 0600, AZOM_DATA_DIR, SQLite integrity/WAL
    missing_cfg = [f for f in CORE_CONFIG_FILES if not (cfg_dir / f).is_file()]
    invalid_yaml: list[str] = []
    if cfg_dir.is_dir():
        for yml_path in sorted(cfg_dir.glob("*.yaml")):
            try:
                yaml.safe_load(yml_path.read_text(encoding="utf-8"))
            except Exception as exc:
                invalid_yaml.append(f"{yml_path.name} ({type(exc).__name__})")
    if missing_cfg or invalid_yaml:
        parts = []
        if missing_cfg:
            parts.append(f"saknas: {', '.join(missing_cfg)}")
        if invalid_yaml:
            parts.append(f"ogiltig YAML: {', '.join(invalid_yaml)}")
        _add(
            CheckItem(
                category="config",
                name="Konfigurationsfiler (YAML)",
                status="fail",
                detail="; ".join(parts),
                fix="Kontrollera AZOM_CONFIG_DIR och korrigera saknade/trasiga YAML-filer.",
            )
        )
    else:
        _add(
            CheckItem(
                category="config",
                name="Konfigurationsfiler (YAML)",
                status="ok",
                detail=f"{cfg_dir} (giltig YAML)",
            )
        )

    env_path = repo_root / ".env"
    if not env_path.is_file():
        _add(
            CheckItem(
                category="config",
                name=".env-fil",
                status="info" if is_mock else "warn",
                detail="saknas (använder process-env / defaults)",
                fix="Skapa .env (t.ex. via --init-env eller install-ubuntu26.sh).",
            )
        )
    else:
        mode_oct = _env_posix_mode(env_path)
        if mode_oct is None:
            _add(CheckItem(category="config", name=".env-fil", status="ok", detail=str(env_path)))
        elif (mode_oct & 0o077) != 0:
            world_open = (mode_oct & 0o007) != 0
            _add(
                CheckItem(
                    category="config",
                    name=".env-fil & behörighet",
                    status="fail" if (not is_mock and world_open) else "warn",
                    detail=f"{env_path} har läsbara rättigheter {oct(mode_oct)} (bör vara 0o600)",
                    fix=f"chmod 600 {env_path}",
                )
            )
        else:
            _add(
                CheckItem(
                    category="config",
                    name=".env-fil & behörighet",
                    status="ok",
                    detail=f"{env_path} (mode {oct(mode_oct)})",
                )
            )

    writable, wr_detail = _dir_writable(data_dir)
    _add(
        CheckItem(
            category="config",
            name="Datakatalog (AZOM_DATA_DIR)",
            status="ok" if writable else "fail",
            detail=wr_detail,
            fix="Säkerställ skrivrättigheter till AZOM_DATA_DIR." if not writable else "",
        )
    )

    db_item, db_meta = _check_sqlite_databases(data_dir)
    details["databases"] = db_meta
    _add(db_item)

    # 5. Production safety & live readiness
    null_send = env_flag("AZOM_NULL_SEND", default=False)
    auto_kill = (os.environ.get("AZOM_AUTO_SEND_KILL") or "").strip()
    auto_send_cfg = False
    cases_ai_path = cfg_dir / "cases_ai.yaml"
    if cases_ai_path.is_file():
        try:
            loaded_ai = yaml.safe_load(cases_ai_path.read_text(encoding="utf-8")) or {}
            if isinstance(loaded_ai, dict):
                auto_send_cfg = bool(loaded_ai.get("auto_send_enabled", False))
        except Exception:
            pass
    safety_status = "warn" if (auto_send_cfg and not auto_kill) else "ok"
    _add(
        CheckItem(
            category="safety",
            name="Körläge & skyddsräcken",
            status=safety_status,
            detail=(
                f"läge={mode_label}, null_send={'på' if null_send else 'av'}, "
                f"auto_send_enabled={'på' if auto_send_cfg else 'av'}, "
                f"auto_send_kill={auto_kill or 'default-off'}"
            ),
            fix="Sätt AZOM_AUTO_SEND_KILL=1 eller auto_send_enabled: false." if safety_status == "warn" else "",
        )
    )

    secret_val = (os.environ.get("DASHBOARD_SECRET_KEY") or "").strip()
    secret_ok = bool(secret_val)
    jonatan_auth = _has_env("DASHBOARD_PASSWORD") or _has_env("DASHBOARD_PASSWORD_HASH")
    oscar_auth = _has_env("DASHBOARD_OSCAR_PASSWORD") or _has_env("DASHBOARD_OSCAR_PASSWORD_HASH")
    pub_url = (os.environ.get("AZOM_DASHBOARD_PUBLIC_URL") or "").strip()
    trusted_raw = (os.environ.get("TRUSTED_PROXY_COUNT") or "").strip()
    trusted_count = int(trusted_raw) if trusted_raw.isdigit() else 0

    if not is_mock and (not secret_ok or not jonatan_auth or not oscar_auth):
        missing_auth = []
        if not secret_ok:
            missing_auth.append("DASHBOARD_SECRET_KEY")
        if not jonatan_auth:
            missing_auth.append("DASHBOARD_PASSWORD/_HASH")
        if not oscar_auth:
            missing_auth.append("DASHBOARD_OSCAR_PASSWORD/_HASH")
        _add(
            CheckItem(
                category="dashboard",
                name="Dashboard säkerhet & autentisering",
                status="fail",
                detail=f"saknas i live-läge: {', '.join(missing_auth)}",
                fix=f"Sätt {', '.join(missing_auth)} i .env.",
            )
        )
    elif not secret_ok:
        _add(
            CheckItem(
                category="dashboard",
                name="Dashboard säkerhet & autentisering",
                status="warn",
                detail="DASHBOARD_SECRET_KEY saknas (mock-fallback aktiv)",
                fix="Sätt DASHBOARD_SECRET_KEY i .env / miljövariabel.",
            )
        )
    elif not is_mock and len(secret_val) < 16:
        _add(
            CheckItem(
                category="dashboard",
                name="Dashboard säkerhet & autentisering",
                status="warn",
                detail="DASHBOARD_SECRET_KEY är kort (< 16 tecken)",
                fix="Generera minst 32 bytes random hex till DASHBOARD_SECRET_KEY.",
            )
        )
    else:
        _add(
            CheckItem(
                category="dashboard",
                name="Dashboard säkerhet & autentisering",
                status="ok",
                detail=(
                    f"secret=satt, jonatan={'satt' if jonatan_auth else 'mock'}, "
                    f"oscar={'satt' if oscar_auth else 'mock'}"
                ),
            )
        )

    if pub_url.lower().startswith("https://") and trusted_count < 1:
        _add(
            CheckItem(
                category="dashboard",
                name="Reverse proxy (TRUSTED_PROXY_COUNT)",
                status="warn" if not is_mock else "info",
                detail=f"AZOM_DASHBOARD_PUBLIC_URL är https:// men TRUSTED_PROXY_COUNT={trusted_count}",
                fix="Sätt TRUSTED_PROXY_COUNT=1 bakom Caddy/Nginx så ProxyFix litar på X-Forwarded-*.",
            )
        )
    elif pub_url:
        _add(
            CheckItem(
                category="dashboard",
                name="Reverse proxy (TRUSTED_PROXY_COUNT)",
                status="warn" if (not is_mock and pub_url.lower().startswith("http://")) else "ok",
                detail=f"public_url={pub_url}, trusted_proxy_count={trusted_count}",
                fix="Använd https:// i AZOM_DASHBOARD_PUBLIC_URL i produktion." if (not is_mock and pub_url.lower().startswith("http://")) else "",
            )
        )

    preferred = _add_model_and_channel_checks(_add, is_mock=is_mock, strict_messenger=True)

    # 6. Opt-in network / domain check (NEVER hits network in mock mode or by default)
    port_raw = (os.environ.get("DASHBOARD_PORT") or "8080").strip()
    dash_port = int(port_raw) if port_raw.isdigit() else 8080
    target_domain = (domain or "").strip()
    if not target_domain and pub_url:
        parsed_pub = urlparse(pub_url)
        target_domain = (parsed_pub.hostname or "").strip()

    if is_mock and (check_network or domain or expected_ip):
        _add(
            CheckItem(
                category="network",
                name="Nätverk & domän",
                status="skip",
                detail="hoppad i mock-läge (inga externa anrop i --mock)",
            )
        )
    elif check_network:
        if check_ports:
            bind_info = _inspect_port_bind(dash_port)
            details["port_bind"] = bind_info
            if bind_info.get("public_bind"):
                _add(
                    CheckItem(
                        category="network",
                        name=f"Port {dash_port} bind-posture",
                        status="warn",
                        detail=str(bind_info["detail"]),
                        fix="Bind DASHBOARD_HOST=127.0.0.1 och exponera via Caddy/Nginx.",
                    )
                )
            else:
                _add(
                    CheckItem(
                        category="network",
                        name=f"Port {dash_port} bind-posture",
                        status="ok" if bind_info.get("listening") else "info",
                        detail=str(bind_info["detail"]),
                    )
                )

        if not target_domain:
            _add(
                CheckItem(
                    category="network",
                    name="DNS A-post & TLS",
                    status="warn",
                    detail="ingen domän angiven (--domain eller AZOM_DASHBOARD_PUBLIC_URL)",
                    fix="Ange --domain <domän> eller sätt AZOM_DASHBOARD_PUBLIC_URL.",
                )
            )
        else:
            try:
                ips = _resolve_dns_a(target_domain)
            except Exception as exc:
                ips = []
                dns_err = _safe_text(f"{type(exc).__name__}: {exc}")
            else:
                dns_err = ""
            details["dns"] = {"domain": target_domain, "ips": ips, "expected_ip": expected_ip or ""}
            if not ips:
                _add(
                    CheckItem(
                        category="network",
                        name="DNS A-post",
                        status="fail",
                        detail=f"{target_domain} löser ej ({dns_err or 'inget A-record'})",
                        fix=f"Peka DNS A-record för {target_domain} till serverns publika IP.",
                    )
                )
            elif expected_ip and expected_ip not in ips:
                _add(
                    CheckItem(
                        category="network",
                        name="DNS A-post",
                        status="fail",
                        detail=f"{target_domain} -> {', '.join(ips)} (förväntade {expected_ip})",
                        fix=f"Uppdatera DNS A-record för {target_domain} till {expected_ip}.",
                    )
                )
            else:
                _add(
                    CheckItem(
                        category="network",
                        name="DNS A-post",
                        status="ok",
                        detail=f"{target_domain} -> {', '.join(ips)}",
                    )
                )

            tls = _probe_tls_cert(target_domain, 443)
            details["tls"] = tls
            if not tls.get("ok"):
                _add(
                    CheckItem(
                        category="network",
                        name="TLS-certifikat (:443)",
                        status="fail",
                        detail=f"{target_domain}: {tls.get('error') or 'certifikat utgånget/ogiltigt'}",
                        fix="Kontrollera att Caddy/Nginx kör och att port 80/443 är öppna i UFW.",
                    )
                )
            else:
                days = tls.get("days_remaining")
                tls_status = "warn" if (days is not None and days < TLS_WARN_DAYS) else "ok"
                _add(
                    CheckItem(
                        category="network",
                        name="TLS-certifikat (:443)",
                        status=tls_status,
                        detail=(
                            f"{target_domain} giltigt ({days} dagar kvar)"
                            if days is not None
                            else f"{target_domain} giltigt"
                        ),
                        fix="Förnya TLS-certifikat snarast." if tls_status == "warn" else "",
                    )
                )

    # 7. Systemd unit enablement check on Linux
    if _is_linux() and _find_binary("systemctl"):
        units = (*AZOM_SERVICES, *AZOM_TIMERS, *PROXY_SERVICES)
        unit_states = _query_systemd_units(units)
        details["systemd"] = unit_states
        if unit_states:
            installed_azom = [
                u for u in (*AZOM_SERVICES, *AZOM_TIMERS)
                if unit_states.get(u, {}).get("load_state") == "loaded"
            ]
            if not installed_azom:
                _add(
                    CheckItem(
                        category="systemd",
                        name="Systemd-enheter",
                        status="info",
                        detail="inga azom-*-enheter installerade i systemd ännu",
                    )
                )
            else:
                disabled_timers = [
                    t for t in AZOM_TIMERS
                    if unit_states.get(t, {}).get("unit_file_state") != "enabled"
                ]
                dash_enabled = unit_states.get("azom-dashboard.service", {}).get("unit_file_state") == "enabled"
                if not dash_enabled or disabled_timers:
                    issues = []
                    if not dash_enabled:
                        issues.append("azom-dashboard.service ej enabled")
                    if disabled_timers:
                        issues.append(f"timers ej enabled: {', '.join(disabled_timers)}")
                    _add(
                        CheckItem(
                            category="systemd",
                            name="Systemd-enheter",
                            status="warn",
                            detail="; ".join(issues),
                            fix="Kör systemctl enable för saknade azom-enheter/timers.",
                        )
                    )
                else:
                    _add(
                        CheckItem(
                            category="systemd",
                            name="Systemd-enheter",
                            status="ok",
                            detail=f"azom-dashboard + {len(AZOM_TIMERS)} timers installerade och enabled",
                        )
                    )

    blockers = [f"{c.name}: {c.detail}" for c in checks if c.status == "fail"]
    warnings = [f"{c.name}: {c.detail}" for c in checks if c.status == "warn"]
    return CheckReport(
        kind="systemcheck",
        version=__version__,
        mode=mode_label,
        os_name=os_label,
        root=str(repo_root),
        data_dir=str(data_dir),
        config_dir=str(cfg_dir),
        preferred_route=preferred,
        checks=checks,
        blockers=blockers,
        warnings=warnings,
        next_steps=steps,
        details=details,
    )


def run_healthcheck(
    root: Path | None = None,
    *,
    mode: str = "auto",
    local_url: str | None = None,
    public_url: str | None = None,
    no_http: bool = False,
    check_public: bool = False,
    strict: bool = False,
    timeout_sec: float = 4.0,
    apply_dotenv: bool = True,
) -> CheckReport:
    """Run live/mock runtime healthcheck (HTTP probes, systemd, runtime snapshot)."""
    del strict  # applied when computing exit_code()
    repo_root = (root or default_repo_root()).resolve()
    os.environ.setdefault("AZOM_CONFIG_DIR", str(repo_root / "config"))
    skip_env = {"AZOM_USE_MOCK"} if mode in {"mock", "live"} else set()
    if apply_dotenv:
        _apply_dotenv_defaults(repo_root, skip_keys=skip_env)
    bootstrap_runtime()

    if mode == "mock":
        os.environ["AZOM_USE_MOCK"] = "1"
    elif mode == "live":
        os.environ["AZOM_USE_MOCK"] = "0"

    is_mock = env_flag("AZOM_USE_MOCK", default=True) if mode == "auto" else (mode == "mock")
    mode_label = "mock" if is_mock else "live"
    cfg_dir, data_dir = resolve_runtime_dirs(repo_root)
    os.environ.setdefault("AZOM_DATA_DIR", str(data_dir))

    checks: list[CheckItem] = []
    steps: list[str] = []
    details: dict[str, Any] = {}

    def _add(item: CheckItem) -> None:
        checks.append(item)
        if item.fix and item.status in {"fail", "warn"} and item.fix not in steps:
            steps.append(item.fix)

    os_label = platform.system()

    # 1. Local dashboard HTTP probes (/live, /health, /ready)
    host = (os.environ.get("DASHBOARD_HOST") or "127.0.0.1").strip()
    if host in {"0.0.0.0", "::", ""}:
        host = "127.0.0.1"
    port_raw = (os.environ.get("DASHBOARD_PORT") or "8080").strip()
    dash_port = int(port_raw) if port_raw.isdigit() else 8080
    base_local = (local_url or f"http://{host}:{dash_port}").rstrip("/")

    if no_http:
        _add(
            CheckItem(
                category="http_local",
                name="Lokal dashboard HTTP",
                status="skip",
                detail="hoppad (--no-http)",
            )
        )
    else:
        live_code, live_json, live_err = _http_get(f"{base_local}/live", timeout=timeout_sec)
        if live_code == 0 and is_mock:
            _add(
                CheckItem(
                    category="http_local",
                    name="Lokal dashboard HTTP",
                    status="skip",
                    detail=f"ej startad på {base_local} (hoppad i mock-läge)",
                )
            )
        else:
            details["http_local"] = {"base_url": base_local}
            if live_code == 200 and isinstance(live_json, dict) and live_json.get("liveness") is True:
                _add(
                    CheckItem(
                        category="http_local",
                        name="Dashboard /live",
                        status="ok",
                        detail=f"HTTP 200 (liveness=true) på {base_local}",
                    )
                )
            else:
                _add(
                    CheckItem(
                        category="http_local",
                        name="Dashboard /live",
                        status="fail",
                        detail=f"HTTP {live_code} ({live_err or 'liveness ej ok'}) på {base_local}",
                        fix="Starta azom-dashboard eller kontrollera journalctl -u azom-dashboard.",
                    )
                )

            h_code, h_json, h_err = _http_get(f"{base_local}/health", timeout=timeout_sec)
            if h_code == 200 and isinstance(h_json, dict) and h_json.get("ok") is True:
                details["http_local"]["health"] = h_json
                null_send = h_json.get("null_send", "unknown")
                b_ratio = h_json.get("budget_ratio")
                b_warn = bool(h_json.get("budget_pacing_warn"))
                poll_errs = int(h_json.get("last_poll_errors") or 0)
                ws_status = h_json.get("web_search_status") or "unknown"
                msg_info = h_json.get("messenger") or {}
                rd_info = h_json.get("readiness") or {}
                _add(
                    CheckItem(
                        category="http_local",
                        name="Dashboard /health",
                        status="warn" if (b_warn or poll_errs > 0) else "ok",
                        detail=(
                            f"HTTP 200, null_send={null_send}, "
                            f"budget_ratio={b_ratio}, poll_errors={poll_errs}, "
                            f"web_search={ws_status}, messenger_cfg={bool(msg_info.get('configured'))}"
                        ),
                        fix=(
                            "Kontrollera senaste mail-pollfel i cases/runtime."
                            if poll_errs > 0
                            else ("OpenRouter budget-pacing varning aktiv." if b_warn else "")
                        ),
                    )
                )
                rd_ok = bool(rd_info.get("ok", False))
                rd_detail = str(rd_info.get("detail") or ("ok" if rd_ok else "ej redo"))
                r_code, r_json, _r_err = _http_get(f"{base_local}/ready", timeout=timeout_sec)
                details["http_local"]["ready_code"] = r_code
                if r_code == 200 and (not isinstance(r_json, dict) or r_json.get("ok", True)):
                    _add(
                        CheckItem(
                            category="http_local",
                            name="Dashboard /ready",
                            status="ok",
                            detail=f"HTTP 200 ({rd_detail})",
                        )
                    )
                else:
                    _add(
                        CheckItem(
                            category="http_local",
                            name="Dashboard /ready",
                            status="warn" if is_mock else "warn",
                            detail=f"HTTP {r_code} ({rd_detail})",
                            fix="Kontrollera azom-cases-poll.timer om poll-markör saknas eller är gammal.",
                        )
                    )
            else:
                _add(
                    CheckItem(
                        category="http_local",
                        name="Dashboard /health",
                        status="fail",
                        detail=f"HTTP {h_code} ({h_err or 'ogiltigt svar'})",
                        fix="Kontrollera att dashboard-processen svarar på /health.",
                    )
                )

    # 2. Opt-in public HTTPS probe
    target_pub = (public_url or os.environ.get("AZOM_DASHBOARD_PUBLIC_URL") or "").strip().rstrip("/")
    if check_public and is_mock:
        _add(
            CheckItem(
                category="http_public",
                name="Publik HTTPS-kontroll",
                status="skip",
                detail="hoppad i mock-läge",
            )
        )
    elif check_public:
        if not target_pub:
            _add(
                CheckItem(
                    category="http_public",
                    name="Publik HTTPS-kontroll",
                    status="warn",
                    detail="AZOM_DASHBOARD_PUBLIC_URL eller --public-url saknas",
                    fix="Sätt AZOM_DASHBOARD_PUBLIC_URL=https://... eller ange --public-url.",
                )
            )
        else:
            parsed = urlparse(target_pub)
            pub_host = parsed.hostname or ""
            pub_port = parsed.port or (443 if parsed.scheme == "https" else 80)
            details["http_public"] = {"url": target_pub}
            if parsed.scheme != "https" or not pub_host:
                _add(
                    CheckItem(
                        category="http_public",
                        name="Publik HTTPS-TLS",
                        status="fail",
                        detail=f"URL måste använda https:// ({target_pub})",
                        fix="Konfigurera HTTPS via Caddy/Nginx.",
                    )
                )
            else:
                tls = _probe_tls_cert(pub_host, pub_port, timeout=timeout_sec)
                details["http_public"]["tls"] = tls
                if not tls.get("ok"):
                    _add(
                        CheckItem(
                            category="http_public",
                            name="Publik HTTPS-TLS",
                            status="fail",
                            detail=f"{pub_host}: {tls.get('error') or 'TLS-certifikat ogiltigt'}",
                            fix="Kontrollera Caddy/Nginx TLS och DNS A-post.",
                        )
                    )
                else:
                    days = tls.get("days_remaining")
                    tls_status = "warn" if (days is not None and days < TLS_WARN_DAYS) else "ok"
                    _add(
                        CheckItem(
                            category="http_public",
                            name="Publik HTTPS-TLS",
                            status=tls_status,
                            detail=f"{pub_host} giltigt ({days} dagar kvar)" if days is not None else f"{pub_host} giltigt",
                            fix="Förnya TLS-certifikat snarast." if tls_status == "warn" else "",
                        )
                    )

            pl_code, pl_json, pl_err = _http_get(f"{target_pub}/live", timeout=timeout_sec)
            ph_code, ph_json, ph_err = _http_get(f"{target_pub}/health", timeout=timeout_sec)
            live_ok = pl_code == 200 and isinstance(pl_json, dict) and pl_json.get("ok") is True
            health_ok = ph_code == 200 and isinstance(ph_json, dict) and ph_json.get("ok") is True
            _add(
                CheckItem(
                    category="http_public",
                    name="Publik /live & /health",
                    status="ok" if (live_ok and health_ok) else "fail",
                    detail=f"/live={pl_code}, /health={ph_code}" + (f" ({pl_err or ph_err})" if (pl_err or ph_err) else ""),
                    fix="Kontrollera reverse proxy routing mot 127.0.0.1:8080." if not (live_ok and health_ok) else "",
                )
            )

            pm_code, _pm_json, pm_err = _http_get(f"{target_pub}/metrics", timeout=timeout_sec)
            metrics_blocked = pm_code in {403, 404}
            _add(
                CheckItem(
                    category="http_public",
                    name="Publik /metrics låsning",
                    status="ok" if metrics_blocked else "fail",
                    detail=f"/metrics HTTP {pm_code}" + (" (blockerad)" if metrics_blocked else f" ({pm_err or 'EJ blockerad!'})"),
                    fix="Blockera /metrics utan token (403/404) i dashboard/reverse proxy." if not metrics_blocked else "",
                )
            )

    # 3. Systemd active state on Linux
    if _is_linux() and _find_binary("systemctl"):
        units = (*AZOM_SERVICES, *AZOM_TIMERS, *PROXY_SERVICES)
        states = _query_systemd_units(units)
        details["systemd"] = states
        if states:
            dash_active = states.get("azom-dashboard.service", {}).get("active_state") == "active"
            dash_loaded = states.get("azom-dashboard.service", {}).get("load_state") == "loaded"
            if dash_loaded:
                _add(
                    CheckItem(
                        category="systemd",
                        name="systemd azom-dashboard",
                        status="ok" if dash_active else "fail",
                        detail=f"active={states.get('azom-dashboard.service', {}).get('active_state')}",
                        fix="Kör systemctl restart azom-dashboard.service" if not dash_active else "",
                    )
                )
            tg_set = _has_env("TELEGRAM_BOT_TOKEN")
            bot_state = states.get("azom-bot.service", {})
            if tg_set and bot_state.get("load_state") == "loaded":
                bot_active = bot_state.get("active_state") == "active"
                _add(
                    CheckItem(
                        category="systemd",
                        name="systemd azom-bot",
                        status="ok" if bot_active else "warn",
                        detail=f"active={bot_state.get('active_state')}",
                        fix="Kör systemctl restart azom-bot.service" if not bot_active else "",
                    )
                )
            elif not tg_set:
                _add(
                    CheckItem(
                        category="systemd",
                        name="systemd azom-bot",
                        status="info",
                        detail="TELEGRAM_BOT_TOKEN saknas — bot ej krävd",
                    )
                )
            loaded_timers = [t for t in AZOM_TIMERS if states.get(t, {}).get("load_state") == "loaded"]
            if loaded_timers:
                inactive_timers = [
                    t for t in loaded_timers
                    if states.get(t, {}).get("active_state") != "active"
                ]
                _add(
                    CheckItem(
                        category="systemd",
                        name="systemd timers",
                        status="warn" if inactive_timers else "ok",
                        detail=(
                            f"{len(loaded_timers)}/{len(AZOM_TIMERS)} aktiva"
                            if not inactive_timers
                            else f"inaktiva timers: {', '.join(inactive_timers)}"
                        ),
                        fix="Kör systemctl enable --now för inaktiva azom-*-timers." if inactive_timers else "",
                    )
                )
            active_proxies = [
                p for p in PROXY_SERVICES
                if states.get(p, {}).get("active_state") == "active"
            ]
            _add(
                CheckItem(
                    category="systemd",
                    name="systemd reverse proxy",
                    status="ok" if active_proxies else "info",
                    detail=f"aktiv: {', '.join(active_proxies)}" if active_proxies else "varken caddy eller nginx aktiv",
                )
            )

    # 4. Runtime snapshot integration
    snap = collect_runtime_snapshot(data_dir=data_dir)
    details["runtime_snapshot"] = snap

    poller = snap.get("poller") or {}
    tg_token_set = _has_env("TELEGRAM_BOT_TOKEN")
    if poller.get("present"):
        p_ok = bool(poller.get("healthy"))
        _add(
            CheckItem(
                category="runtime_snapshot",
                name="Telegram poller-lease",
                status="ok" if p_ok else "warn",
                detail=f"status={poller.get('status')}, ttl={poller.get('ttl_seconds', 0)}s",
                fix="Kontrollera azom-bot.service om poller-lease gått ut." if not p_ok else "",
            )
        )
    elif tg_token_set and not is_mock:
        _add(
            CheckItem(
                category="runtime_snapshot",
                name="Telegram poller-lease",
                status="info",
                detail="ingen aktiv poller-lease registrerad ännu",
            )
        )

    queues = snap.get("queues") or {}
    tg_q = queues.get("telegram") or {}
    ms_q = queues.get("messenger") or {}
    res_q = queues.get("research") or {}
    q_failed = int(tg_q.get("failed") or 0) + int(ms_q.get("failed") or 0) + int(res_q.get("failed") or 0)
    _add(
        CheckItem(
            category="runtime_snapshot",
            name="Jobbköer (TG/Messenger/Research)",
            status="warn" if q_failed > 0 else "ok",
            detail=(
                f"tg(q={tg_q.get('queued', 0)},r={tg_q.get('running', 0)},f={tg_q.get('failed', 0)}), "
                f"msg(p={ms_q.get('pending', 0)},r={ms_q.get('running', 0)},f={ms_q.get('failed', 0)}), "
                f"res(q={res_q.get('queued', 0)},r={res_q.get('running', 0)},f={res_q.get('failed', 0)})"
            ),
            fix="Inspektera misslyckade jobb i /ops/runtime." if q_failed > 0 else "",
        )
    )

    mr = snap.get("model_router") or {}
    sr = snap.get("search_router") or {}
    open_mr = int(mr.get("open_breakers_count") or 0)
    open_sr = int(sr.get("open_breakers_count") or 0)
    oauth_meta = mr.get("chatgpt_oauth") or {}
    oauth_status = str(oauth_meta.get("status") or "disconnected")
    oauth_ttl = int(oauth_meta.get("ttl_seconds") or 0)
    _add(
        CheckItem(
            category="runtime_snapshot",
            name="Breakers & ChatGPT OAuth TTL",
            status="warn" if (open_mr > 0 or open_sr > 0 or oauth_status in {"needs_reauth", "expired"}) else "ok",
            detail=f"model_open={open_mr}, search_open={open_sr}, chatgpt_oauth={oauth_status} (ttl={oauth_ttl}s)",
            fix=(
                "Återanslut OAuth eller nollställ breakers i /oscar."
                if (open_mr > 0 or open_sr > 0 or oauth_status in {"needs_reauth", "expired"})
                else ""
            ),
        )
    )

    disk_snap = snap.get("disk_usage") or {}
    expired_items = (
        int(disk_snap.get("expired_plans_count") or 0)
        + int(disk_snap.get("untracked_pv_count") or 0)
        + int(disk_snap.get("expired_research_artifacts_count") or 0)
    )
    _add(
        CheckItem(
            category="runtime_snapshot",
            name="Datakatalog & rensning",
            status="ok",
            detail=(
                f"{disk_snap.get('total_mb', 0.0):.2f} MB i AZOM_DATA_DIR, "
                f"utgångna objekt={expired_items}"
            ),
            fix="Kör python -m ecom_ops --actor oscar ops gc" if expired_items > 0 else "",
        )
    )

    recent_errs = snap.get("recent_errors") or []
    recent_total = int(snap.get("recent_errors_total") or 0)
    if recent_errs:
        newest = recent_errs[0]
        _add(
            CheckItem(
                category="runtime_snapshot",
                name="Senaste runtime-fel",
                status="warn",
                detail=f"{recent_total} loggade fel (senaste: {newest.get('source')}/{newest.get('error_class')})",
                fix="Granska /ops/runtime eller runtime_errors.jsonl.",
            )
        )
    else:
        _add(
            CheckItem(
                category="runtime_snapshot",
                name="Senaste runtime-fel",
                status="ok",
                detail="0 registrerade runtime-fel",
            )
        )

    blockers = [f"{c.name}: {c.detail}" for c in checks if c.status == "fail"]
    warnings = [f"{c.name}: {c.detail}" for c in checks if c.status == "warn"]
    return CheckReport(
        kind="healthcheck",
        version=__version__,
        mode=mode_label,
        os_name=os_label,
        root=str(repo_root),
        data_dir=str(data_dir),
        config_dir=str(cfg_dir),
        checks=checks,
        blockers=blockers,
        warnings=warnings,
        next_steps=steps,
        details=details,
    )


def format_report(report: CheckReport) -> str:
    """Format a CheckReport as concise Swedish operator output (secret-free)."""
    title = "Djup systemkontroll (systemcheck)" if report.kind == "systemcheck" else "Hälsokontroll (healthcheck)"
    counts = report.summary_counts()
    lines = [
        f"AzomOps v{report.version} — {title} ({report.mode})",
        f"Rot: {report.root}",
        f"Data: {report.data_dir}",
        f"Konfig: {report.config_dir}",
        "",
    ]
    for item in report.checks:
        badge = _STATUS_BADGE.get(item.status, "[INFO]")
        lines.append(f"{badge:6s} {item.name}: {item.detail}")
    lines.append("")
    lines.append(
        f"Summering: ok={counts['ok']}, warn={counts['warn']}, fel={counts['fail']}, "
        f"info={counts['info']}, skip={counts['skip']} (status={report.status})"
    )
    if report.blockers:
        lines.append("")
        lines.append(f"Blockerare ({len(report.blockers)}):")
        for b in report.blockers:
            lines.append(f"- {b}")
    if report.next_steps:
        lines.append("")
        lines.append("Nästa steg:")
        for idx, step in enumerate(report.next_steps, start=1):
            lines.append(f"  {idx}. {step}")
    return "\n".join(lines)
