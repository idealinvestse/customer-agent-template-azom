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
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ecom_ops.integrations.wp.process_env import (
    WP_VERSION_RE as _WP_VERSION_RE,
)
from ecom_ops.integrations.wp.process_env import (
    build_php_child_env,
    probe_subprocess_env,
    terminate_process_tree,
)
from ecom_ops.integrations.wp.process_env import (
    pick_loopback_port as _pick_loopback_port,
)
from ecom_ops.integrations.wp.sandbox_sqlite import (
    PLUGIN_ENTRY_RE as _PLUGIN_ENTRY_RE,
)
from ecom_ops.integrations.wp.sandbox_sqlite import (
    init_sqlite_db as _init_sqlite_db,
)
from ecom_ops.integrations.wp_runtime_fetch import RuntimeFetchError, check_runtime, default_dest
from ecom_ops.wp_ops.review_runner import SandboxGate, sandbox_enabled
from ecom_ops.wp_ops.review_workspace import SLUG, ReviewError

_STARTUP_TIMEOUT_S = 15.0
_PLUGIN_MAIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.php$")
_PHP_SYS_VERSION_RE = re.compile(r"PHP (8\.[2-9]\.\d+)")
_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_FACADE_MODULE = "ecom_ops.integrations.wp_local_runtime"
_PINNED_PHP_EXTENSIONS = ("pdo_sqlite", "sqlite3", "mbstring", "fileinfo")
_SYNTHETIC_THEME_FILES = ("style.css", "functions.php", "index.php")


def _facade_attr(name: str, default: Any) -> Any:
    facade = sys.modules.get(_FACADE_MODULE)
    if facade is not None and hasattr(facade, name):
        return getattr(facade, name)
    return default


def _load_template(relative_path: str) -> str:
    return (_TEMPLATES_DIR / relative_path).read_text(encoding="utf-8")


def runtime_dir_from_env(explicit: Path | None = None) -> Path:
    """Resolve the pinned runtime directory from an explicit arg, env, or default."""
    if explicit is not None:
        return Path(explicit)
    os_mod = _facade_attr("os", os)
    configured = os_mod.environ.get("AZOM_WP_RUNTIME_DIR", "").strip()
    if configured:
        return Path(configured)
    return _facade_attr("default_dest", default_dest)()


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


def _write_minimal_synthetic_theme(dest: Path) -> None:
    """Write a minimal synthetic host theme for isolated plugin-only runs."""
    dest.mkdir(parents=True, exist_ok=True)
    for filename in _SYNTHETIC_THEME_FILES:
        (dest / filename).write_text(_load_template(f"synthetic_theme/{filename}"), encoding="utf-8")


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
    os_mod = _facade_attr("os", os)
    check_fn = _facade_attr("check_runtime", check_runtime)
    shutil_mod = _facade_attr("shutil", shutil)
    subprocess_mod = _facade_attr("subprocess", subprocess)
    try:
        return check_fn(resolved_dir)
    except RuntimeFetchError:
        if os_mod.name == "nt":
            raise ReviewError("SandboxRuntimeUnavailable") from None
    except OSError:
        raise ReviewError("SandboxRuntimeUnavailable") from None

    wp_dir = resolved_dir / "wordpress"
    version_file = wp_dir / "wp-includes" / "version.php"
    php_path = shutil_mod.which("php")
    if not version_file.is_file() or version_file.is_symlink() or not php_path:
        raise ReviewError("SandboxRuntimeUnavailable")

    probe_env = probe_subprocess_env()
    try:
        ver_proc = subprocess_mod.run(
            [php_path, "-v"],
            shell=False,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env=probe_env,
        )
        mod_proc = subprocess_mod.run(
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


def _render_db_dropin() -> str:
    return _load_template("db_dropin.php")


def _render_egress_guard() -> str:
    return _load_template("egress_guard.php")


def _render_wp_config(
    *,
    wp_core: Path,
    wp_content: Path,
    base_url: str,
    sqlite_path: Path,
    debug_log: Path,
    egress_log: Path,
) -> str:
    replacements = {
        "__URL__": _php_escape(base_url.rstrip("/")),
        "__WP_CONTENT__": _php_escape(str(wp_content.resolve()).replace("\\", "/")),
        "__SQLITE__": _php_escape(str(sqlite_path.resolve()).replace("\\", "/")),
        "__EGRESS__": _php_escape(str(egress_log.resolve()).replace("\\", "/")),
        "__DEBUG__": _php_escape(str(debug_log.resolve()).replace("\\", "/")),
        "__WP_CORE__": _php_escape(str(wp_core.resolve()).replace("\\", "/") + "/"),
    }
    rendered = _load_template("wp_config.php.tmpl")
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


def _render_router(*, wp_core: Path, wp_content: Path, wp_config: Path) -> str:
    replacements = {
        "__WP_CORE__": _php_escape(str(wp_core.resolve()).replace("\\", "/")),
        "__WP_CONTENT__": _php_escape(str(wp_content.resolve()).replace("\\", "/")),
        "__WP_CONFIG__": _php_escape(str(wp_config.resolve()).replace("\\", "/")),
    }
    rendered = _load_template("router.php.tmpl")
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


def _build_php_command(
    php_binary: Path,
    *,
    ext_dir: Path,
    system_php: bool,
    php_error_log: Path,
    port: int,
    wp_core: Path,
    router: Path,
) -> list[str]:
    command: list[str] = [str(php_binary)]
    if not system_php:
        command.extend(["-n", "-d", f"extension_dir={ext_dir}"])
        for ext in _PINNED_PHP_EXTENSIONS:
            command.extend(["-d", f"extension={ext}"])
    command.extend(
        [
            "-d",
            "display_errors=0",
            "-d",
            "log_errors=1",
            "-d",
            f"error_log={php_error_log}",
            "-S",
            f"127.0.0.1:{port}",
            "-t",
            str(wp_core),
            str(router),
        ]
    )
    return command


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
        resolve_fn = _facade_attr("resolve_local_runtime", resolve_local_runtime)
        os_mod = _facade_attr("os", os)
        subprocess_mod = _facade_attr("subprocess", subprocess)
        try:
            manifest = resolve_fn(self.runtime_dir)
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
        command = _build_php_command(
            php_binary,
            ext_dir=ext_dir,
            system_php=(manifest.get("system_php") is True),
            php_error_log=self._php_error_log,
            port=self.port,
            wp_core=wp_core,
            router=router,
        )
        stdout_path = self.sandbox_dir / "php-server.out.log"
        stderr_path = self.sandbox_dir / "php-server.err.log"
        self._stdout_handle = stdout_path.open("wb")
        self._stderr_handle = stderr_path.open("wb")
        popen_kwargs: dict[str, Any] = {}
        if os_mod.name != "nt":
            popen_kwargs["start_new_session"] = True
        try:
            self._proc = subprocess_mod.Popen(
                command,
                shell=False,
                stdout=self._stdout_handle,
                stderr=self._stderr_handle,
                cwd=str(self.sandbox_dir),
                env=env,
                **popen_kwargs,
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
        try:
            terminate_process_tree(proc, timeout_s=5.0)
        finally:
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


__all__ = [
    "LocalWordPressSandbox",
    "_PLUGIN_ENTRY_RE",
    "_PLUGIN_MAIN_RE",
    "_PHP_SYS_VERSION_RE",
    "_STARTUP_TIMEOUT_S",
    "_build_php_command",
    "_copy_component_tree",
    "_detect_plugin_main_file",
    "_php_escape",
    "_render_db_dropin",
    "_render_egress_guard",
    "_render_router",
    "_render_wp_config",
    "_write_minimal_synthetic_theme",
    "make_local_sandbox_gate",
    "resolve_local_runtime",
    "runtime_dir_from_env",
]
