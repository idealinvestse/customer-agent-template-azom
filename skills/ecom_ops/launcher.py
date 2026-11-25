"""Azom preflight checker and cross-platform system launcher.

Usage:
    python -m ecom_ops.launcher              # Interactive menu (TTY) or preflight check
    python -m ecom_ops.launcher --check      # Run system & OAuth preflight checks
    python -m ecom_ops.launcher --init-env   # Create data dirs and .env from .env.example
    python -m ecom_ops.launcher --target all # Launch dashboard (+ Telegram bot if configured)
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ecom_ops import __version__
from ecom_ops.budget import budget_status
from ecom_ops.model_router import circuit_breaker, get_model_router, reset_model_router
from ecom_ops.runtime_env import _parse_env_file, bootstrap_runtime, env_flag

_REQUIRED_PACKAGES: tuple[tuple[str, str], ...] = (
    ("yaml", "PyYAML"),
    ("requests", "requests"),
    ("flask", "Flask"),
)
_OPTIONAL_PACKAGES: tuple[tuple[str, str], ...] = (
    ("waitress", "waitress"),
    ("paramiko", "paramiko"),
)
_CONFIG_FILES = (
    "sites.yaml",
    "prompts.yaml",
    "model_profiles.yaml",
    "limits.yaml",
)
_STATUS_BADGE = {
    "ok": "[OK]",
    "warn": "[WARN]",
    "fail": "[FEL]",
    "info": "[INFO]",
}


@dataclass(frozen=True)
class CheckItem:
    category: str
    name: str
    status: str  # ok | warn | fail | info
    detail: str
    fix: str = ""


@dataclass
class PreflightReport:
    version: str
    mode: str  # mock | live
    root: str
    data_dir: str
    config_dir: str
    preferred_route: str = ""
    checks: list[CheckItem] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "mode": self.mode,
            "root": self.root,
            "data_dir": self.data_dir,
            "config_dir": self.config_dir,
            "preferred_route": self.preferred_route,
            "ready": len(self.blockers) == 0,
            "checks": [asdict(c) for c in self.checks],
            "blockers": list(self.blockers),
            "warnings": list(self.warnings),
            "next_steps": list(self.next_steps),
        }


def default_repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


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


def _apply_dotenv_defaults(root: Path) -> None:
    env_file = root / ".env"
    if not env_file.is_file():
        return
    for k, v in _parse_env_file(env_file).items():
        if not (os.environ.get(k) or "").strip() and v:
            os.environ[k] = v


def _resolved_dirs(root: Path) -> tuple[Path, Path]:
    cfg_raw = (os.environ.get("AZOM_CONFIG_DIR") or "").strip()
    data_raw = (os.environ.get("AZOM_DATA_DIR") or "").strip()
    cfg_dir = Path(cfg_raw) if cfg_raw else root / "config"
    data_dir = Path(data_raw) if data_raw else root / ".azom-data"
    return cfg_dir, data_dir


def _dir_writable(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path), prefix=".launcher_probe_", suffix=".tmp")
        os.close(fd)
        os.unlink(tmp_name)
        return True, str(path)
    except OSError as exc:
        return False, f"ej skrivbar ({type(exc).__name__})"


def _port_in_use(host: str, port: int) -> bool:
    probe_host = "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host
    try:
        with socket.create_connection((probe_host, port), timeout=0.35):
            return True
    except OSError:
        return False


def _has_env(name: str) -> bool:
    return bool((os.environ.get(name) or "").strip())


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


def run_preflight(
    root: Path | None = None,
    *,
    mode: str = "auto",
    check_ports: bool = True,
) -> PreflightReport:
    """Inspect environment, files, model OAuth/routes, dashboard, and channels."""
    repo_root = (root or default_repo_root()).resolve()
    os.environ.setdefault("AZOM_CONFIG_DIR", str(repo_root / "config"))
    os.environ.setdefault("AZOM_DATA_DIR", str(repo_root / ".azom-data"))
    _apply_dotenv_defaults(repo_root)
    bootstrap_runtime()

    if mode == "mock":
        os.environ["AZOM_USE_MOCK"] = "1"
    elif mode == "live":
        os.environ["AZOM_USE_MOCK"] = "0"

    is_mock = env_flag("AZOM_USE_MOCK", default=True) if mode == "auto" else (mode == "mock")
    mode_label = "mock" if is_mock else "live"
    cfg_dir, data_dir = _resolved_dirs(repo_root)

    checks: list[CheckItem] = []
    steps: list[str] = []

    def _add(item: CheckItem) -> None:
        checks.append(item)
        if item.fix and item.status in {"fail", "warn"} and item.fix not in steps:
            steps.append(item.fix)

    # 1. Python & packages
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
    missing_pkgs = _missing_packages(target_py, _REQUIRED_PACKAGES)
    _add(
        CheckItem(
            category="runtime",
            name="Python-paket",
            status="ok" if not missing_pkgs else "fail",
            detail="kärnpaket installerade" if not missing_pkgs else f"saknas: {', '.join(missing_pkgs)}",
            fix=f"{target_py} -m pip install {' '.join(missing_pkgs)}" if missing_pkgs else "",
        )
    )
    missing_opt = _missing_packages(target_py, _OPTIONAL_PACKAGES)
    if missing_opt:
        _add(
            CheckItem(
                category="runtime",
                name="Produktionspaket (WSGI/SSH)",
                status="warn",
                detail=f"saknas: {', '.join(missing_opt)} (dev fallback aktiv)",
                fix=f"{target_py} -m pip install {' '.join(missing_opt)}",
            )
        )
    _add(
        CheckItem(
            category="runtime",
            name="Virtuell miljö (.venv)",
            status="ok" if venv_py is not None else "info",
            detail=str(venv_py) if venv_py is not None else f"använder system-Python ({sys.executable})",
        )
    )

    # 2. Config files, .env, and data dir
    missing_cfg = [f for f in _CONFIG_FILES if not (cfg_dir / f).is_file()]
    _add(
        CheckItem(
            category="config",
            name="Konfigurationsfiler",
            status="ok" if not missing_cfg else "fail",
            detail=f"{cfg_dir} ok" if not missing_cfg else f"saknas i {cfg_dir}: {', '.join(missing_cfg)}",
            fix="Kontrollera AZOM_CONFIG_DIR eller återställ config/-filer." if missing_cfg else "",
        )
    )
    env_path = repo_root / ".env"
    _add(
        CheckItem(
            category="config",
            name=".env-fil",
            status="ok" if env_path.is_file() else ("info" if is_mock else "warn"),
            detail=str(env_path) if env_path.is_file() else "saknas (använder process-env / defaults)",
            fix="" if env_path.is_file() else "Kör --init-env för att skapa .env från .env.example.",
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

    # 3. Safety posture
    null_send = env_flag("AZOM_NULL_SEND", default=False)
    auto_kill = (os.environ.get("AZOM_AUTO_SEND_KILL") or "").strip()
    _add(
        CheckItem(
            category="safety",
            name="Körläge & skyddsräcken",
            status="ok",
            detail=(
                f"läge={mode_label}, null_send={'på' if null_send else 'av'}, "
                f"auto_send_kill={auto_kill or 'default-off'}"
            ),
        )
    )

    # 4. Model router, OAuth & circuit breaker
    reset_model_router()
    router = get_model_router()
    routes = router.routes()
    preferred = router.preferred_route_id()
    cg = routes.get("chatgpt_oauth") or {}
    cg_ready = bool(cg.get("connected")) and not bool(cg.get("needs_reauth"))
    cg_plan = "plan=ja" if cg.get("plan_usage") else "plan=nej"
    if cg.get("needs_reauth"):
        cg_status = "warn"
        cg_fix = "Återanslut ChatGPT OAuth via /oscar eller Telegram /oauth chatgpt."
    elif cg_ready:
        cg_status = "ok"
        cg_fix = ""
    else:
        cg_status = "info"
        cg_fix = ""
    _add(
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
    _add(
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
    _add(
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
        _add(
            CheckItem(
                category="models",
                name="Aktiv modellrutt",
                status="warn",
                detail="ingen anropbar LLM-rutt ännu (mall-fallback aktiv)",
                fix="Anslut ChatGPT via Telegram (/oauth chatgpt), xAI (/oauth xai), eller sätt OPENROUTER_API_KEY.",
            )
        )
    else:
        _add(
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
    _add(
        CheckItem(
            category="models",
            name="Circuit breaker",
            status="warn" if breaker_open else "ok",
            detail="alla stängda" if not breaker_open else f"öppna: {', '.join(sorted(breaker_open))}",
            fix="Återanslut berörd OAuth eller kör Nollställ circuit breaker i /oscar." if breaker_open else "",
        )
    )

    # 5. Dashboard
    host = (os.environ.get("DASHBOARD_HOST") or "127.0.0.1").strip()
    port_raw = (os.environ.get("DASHBOARD_PORT") or "8080").strip()
    port = int(port_raw) if port_raw.isdigit() else 8080
    secret_ok = _has_env("DASHBOARD_SECRET_KEY")
    in_use = _port_in_use(host, port) if check_ports else False
    dash_status = "fail" if (not is_mock and not secret_ok) else ("warn" if (not secret_ok or in_use) else "ok")
    dash_fix = ""
    if not secret_ok:
        dash_fix = "Sätt DASHBOARD_SECRET_KEY i .env / miljövariabel."
    elif in_use:
        dash_fix = f"Port {port} svarar redan; byt DASHBOARD_PORT eller använd pågående dashboard."
    _add(
        CheckItem(
            category="dashboard",
            name="Dashboard",
            status=dash_status,
            detail=(
                f"http://{host}:{port}, secret={'satt' if secret_ok else 'saknas'}"
                + (", port upptagen" if in_use else "")
            ),
            fix=dash_fix,
        )
    )

    # 6. Telegram & Messenger
    tg_token = _has_env("TELEGRAM_BOT_TOKEN")
    tg_allow = _has_env("TELEGRAM_ALLOWED_CHAT_IDS")
    tg_map = _has_env("TELEGRAM_ACTOR_MAP")
    if tg_token and not is_mock and (not tg_allow or not tg_map):
        tg_status = "fail"
        tg_fix = "Sätt både TELEGRAM_ALLOWED_CHAT_IDS och TELEGRAM_ACTOR_MAP i live-läge (fail-closed)."
    elif tg_token:
        tg_status = "ok"
        tg_fix = ""
    else:
        tg_status = "info"
        tg_fix = "Sätt TELEGRAM_BOT_TOKEN i .env om Telegram-botten ska startas."
    _add(
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

    msg_token = _has_env("MESSENGER_PAGE_ACCESS_TOKEN")
    msg_sec = _has_env("MESSENGER_APP_SECRET")
    msg_ver = _has_env("MESSENGER_VERIFY_TOKEN")
    msg_allow = _has_env("MESSENGER_ALLOWED_PSIDS")
    msg_map = _has_env("MESSENGER_ACTOR_MAP")
    msg_any = msg_token or msg_sec or msg_ver
    msg_all = msg_token and msg_sec and msg_ver
    if msg_any and not is_mock and (not msg_all or not msg_allow or not msg_map):
        msg_status = "warn"
        msg_fix = "Komplettera MESSENGER_PAGE_ACCESS_TOKEN, APP_SECRET, VERIFY_TOKEN, ALLOWED_PSIDS och ACTOR_MAP."
    elif msg_all:
        msg_status = "ok"
        msg_fix = ""
    else:
        msg_status = "info"
        msg_fix = ""
    _add(
        CheckItem(
            category="channels",
            name="Meta Messenger",
            status=msg_status,
            detail="konfigurerad på dashboard webhook" if msg_all else ("delvis satt" if msg_any else "ej konfigurerad"),
            fix=msg_fix,
        )
    )

    # 7. Mail & WooCommerce
    mail_prov = (os.environ.get("MAIL_PROVIDER") or "gmail").strip().lower()
    woo_ready = _has_env("WOO_SE_BASE_URL") or _has_env("WOO_BASE_URL")
    _add(
        CheckItem(
            category="integrations",
            name="Mail & WooCommerce",
            status="ok",
            detail=(
                f"mail={mail_prov}, "
                f"woo={'konfigurerad' if woo_ready else ('mock' if is_mock else 'ej satt')}"
            ),
        )
    )

    blockers = [f"{c.name}: {c.detail}" for c in checks if c.status == "fail"]
    warnings = [f"{c.name}: {c.detail}" for c in checks if c.status == "warn"]
    return PreflightReport(
        version=__version__,
        mode=mode_label,
        root=str(repo_root),
        data_dir=str(data_dir),
        config_dir=str(cfg_dir),
        preferred_route=preferred,
        checks=checks,
        blockers=blockers,
        warnings=warnings,
        next_steps=steps,
    )


def format_report(report: PreflightReport) -> str:
    lines = [
        f"AzomOps v{report.version} — Systemkontroll ({report.mode})",
        f"Rot: {report.root}",
        f"Data: {report.data_dir}",
        "",
    ]
    for item in report.checks:
        badge = _STATUS_BADGE.get(item.status, "[INFO]")
        lines.append(f"{badge:6s} {item.name}: {item.detail}")
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


def init_environment(root: Path | None = None) -> list[str]:
    """Create local data/logs folders and .env from .env.example if missing."""
    repo_root = (root or default_repo_root()).resolve()
    cfg_dir, data_dir = _resolved_dirs(repo_root)
    del cfg_dir
    created: list[str] = []
    for folder in (data_dir, repo_root / "logs"):
        if not folder.exists():
            folder.mkdir(parents=True, exist_ok=True)
            created.append(f"Skapade katalog: {folder}")
    env_file = repo_root / ".env"
    example = repo_root / ".env.example"
    if not env_file.is_file() and example.is_file():
        env_file.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        try:
            os.chmod(env_file, 0o600)
        except OSError:
            pass
        created.append(f"Kopierade {example.name} -> {env_file}")
    elif env_file.is_file():
        created.append(f".env finns redan: {env_file} (rördes inte)")
    return created


def build_launch_commands(
    root: Path,
    *,
    target: str,
    python_exe: str | None = None,
) -> list[tuple[str, list[str]]]:
    py = _resolve_python(root, python_exe=python_exe)
    dash_cmd = ("dashboard", [py, str(root / "infrastructure" / "dashboard" / "app.py")])
    bot_cmd = ("telegram-bot", [py, "-m", "ecom_ops.bot"])
    if target == "dashboard":
        return [dash_cmd]
    if target == "bot":
        return [bot_cmd]
    cmds = [dash_cmd]
    if _has_env("TELEGRAM_BOT_TOKEN"):
        cmds.append(bot_cmd)
    return cmds


def _child_env(root: Path, *, mode: str) -> dict[str, str]:
    env = dict(os.environ)
    skills = str(root / "skills")
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{skills}{os.pathsep}{existing_pp}" if existing_pp else skills
    env.setdefault("AZOM_CONFIG_DIR", str(root / "config"))
    env.setdefault("AZOM_DATA_DIR", str(root / ".azom-data"))
    if mode == "mock":
        env["AZOM_USE_MOCK"] = "1"
    elif mode == "live":
        env["AZOM_USE_MOCK"] = "0"
    return env


def run_mock_smoke(root: Path | None = None) -> int:
    """Run fast local mock verification commands via python -m ecom_ops."""
    repo_root = (root or default_repo_root()).resolve()
    py = _resolve_python(repo_root)
    env = _child_env(repo_root, mode="mock")
    smoke_cmds = [
        [py, "-m", "ecom_ops", "--mock", "order-status", "--order-id", "1001", "--status", "completed"],
        [py, "-m", "ecom_ops", "--mock", "support", "--message", "Order 1001 status?"],
        [py, "-m", "ecom_ops", "--mock", "mail", "fetch"],
    ]
    for cmd in smoke_cmds:
        proc = subprocess.run(cmd, cwd=str(repo_root), env=env, check=False)
        if proc.returncode != 0:
            return proc.returncode
    return 0


def launch_targets(
    root: Path | None = None,
    *,
    target: str = "all",
    mode: str = "auto",
    dry_run: bool = False,
) -> int:
    repo_root = (root or default_repo_root()).resolve()
    commands = build_launch_commands(repo_root, target=target)
    if target == "all" and len(commands) == 1:
        print("Obs: TELEGRAM_BOT_TOKEN saknas — startar Dashboard (hoppar över Telegram-bot).")
    env = _child_env(repo_root, mode=mode)
    if dry_run:
        for label, cmd in commands:
            print(f"[DRY-RUN] {label}: {' '.join(cmd)}")
        return 0

    procs: list[tuple[str, subprocess.Popen[bytes]]] = []
    try:
        for label, cmd in commands:
            print(f"Startar {label}...")
            proc = subprocess.Popen(cmd, cwd=str(repo_root), env=env)
            procs.append((label, proc))
        while True:
            for label, proc in procs:
                rc = proc.poll()
                if rc is not None:
                    print(f"{label} avslutades med kod {rc}.")
                    return rc
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStoppar tjänster...")
        return 0
    finally:
        for _label, proc in procs:
            if proc.poll() is None:
                proc.terminate()
        for _label, proc in procs:
            if proc.poll() is None:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()


def _interactive_menu(root: Path, report: PreflightReport, *, mode: str) -> int:
    while True:
        print("")
        print("Välj åtgärd:")
        print("  1) Starta systemet (Dashboard" + (" + Telegram-bot)" if _has_env("TELEGRAM_BOT_TOKEN") else ")"))
        print("  2) Starta endast Dashboard (:8080)")
        print("  3) Starta endast Telegram-bot")
        print("  4) Initiera .env och datakataloger")
        print("  5) Kör snabb mock-smoke")
        print("  6) Kör om systemkontroll")
        print("  0) Avsluta")
        try:
            choice = input("Val [0-6]: ").strip()
        except EOFError:
            return 0 if not report.blockers else 1
        if choice == "0":
            return 0 if not report.blockers else 1
        if choice in {"1", "2", "3"}:
            tgt = {"1": "all", "2": "dashboard", "3": "bot"}[choice]
            if report.blockers:
                print("Kan inte starta: åtgärda blockerare ovan först.")
                continue
            return launch_targets(root, target=tgt, mode=mode, dry_run=False)
        if choice == "4":
            for msg in init_environment(root):
                print(f"- {msg}")
            report = run_preflight(root, mode=mode)
            print(format_report(report))
            continue
        if choice == "5":
            rc = run_mock_smoke(root)
            print("Mock-smoke OK!" if rc == 0 else f"Mock-smoke misslyckades (kod {rc}).")
            continue
        if choice == "6":
            report = run_preflight(root, mode=mode)
            print(format_report(report))
            continue
        print("Ogiltigt val.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ecom_ops.launcher",
        description="AzomOps preflight-kontroll och launcher (Dashboard + Telegram-bot).",
    )
    parser.add_argument("--check", action="store_true", help="Kör systemkontroll och avsluta")
    parser.add_argument("--json", action="store_true", help="Skriv preflight-rapport som JSON")
    parser.add_argument("--init-env", action="store_true", help="Skapa data/logs-mappar och .env från .env.example")
    parser.add_argument("--mode", choices=("auto", "mock", "live"), default="auto", help="Tvinga mock eller live")
    parser.add_argument("--target", choices=("all", "dashboard", "bot"), help="Vilken tjänst som ska startas")
    parser.add_argument("--dry-run", action="store_true", help="Visa startkommandon utan att starta processer")
    parser.add_argument("--smoke", action="store_true", help="Kör lokal mock-smoke")
    parser.add_argument("--allow-incomplete", action="store_true", help="Starta trots preflight-blockerare")
    parser.add_argument("--no-port-check", action="store_true", help="Hoppa över TCP-portkontroll")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = default_repo_root()

    if args.init_env:
        for msg in init_environment(root):
            print(msg)

    if args.smoke:
        return run_mock_smoke(root)

    report = run_preflight(root, mode=args.mode, check_ports=not args.no_port_check)
    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(format_report(report))

    if args.check:
        return 0 if not report.blockers else 1

    if args.target:
        if report.blockers and not args.allow_incomplete and not args.dry_run:
            print("Avbryter start pga preflight-blockerare (använd --allow-incomplete för att tvinga).", file=sys.stderr)
            return 1
        return launch_targets(root, target=args.target, mode=args.mode, dry_run=args.dry_run)

    if not args.json and sys.stdin.isatty() and not args.init_env:
        return _interactive_menu(root, report, mode=args.mode)

    return 0 if not report.blockers else 1


if __name__ == "__main__":
    raise SystemExit(main())
