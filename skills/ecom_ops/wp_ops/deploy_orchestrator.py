"""Safe live/staging deploy orchestrator with pre-change snapshots and auto-rollback.

Preserves existing fail-closed ``BackupNotVerified`` behavior on legacy
``request_production`` routes while offering gated, verified deployment through
either ``azom-ops-bridge`` (``AZOM_WP_BRIDGE_ENABLED=1``) or ``SshDeployTransport``
(``AZOM_WP_DEPLOY_SSH=1``) when ``AZOM_WP_LIVE_DEPLOY_ENABLED=1``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ecom_ops.audit import log_action
from ecom_ops.integrations.wp.bridge_client import (
    WpBridgeClient,
    bridge_client_for_site,
    bridge_enabled,
)
from ecom_ops.integrations.wp.sftp_deploy_transport import (
    SshDeployTransport,
    deploy_ssh_enabled,
)
from ecom_ops.rbac import Permission
from ecom_ops.runtime_env import env_flag
from ecom_ops.wp_ops.change_ledger import ChangeLedger
from ecom_ops.wp_ops.config import data_dir_path, is_mock_mode
from ecom_ops.wp_ops.file_paths import (
    resolve_file,
    resolve_mu_root_file,
    sha256_digest,
)
from ecom_ops.wp_ops.models import WpOpsResult, fail_result, ok_result
from ecom_ops.wp_ops.review.plans import ReviewPlans
from ecom_ops.wp_ops.review.store import _SANDBOX_ONLY_FILES, _TERMINAL, _diff_ops
from ecom_ops.wp_ops.review.workspace_io import workspace_root, write_manifest


def _resolve_deploy_abs(files_root: str, kind: str, slug: str, rel: str) -> str:
    if kind == "mu_plugin" and slug in {"", "_root", "mu-plugins"}:
        return resolve_mu_root_file(files_root, rel)
    return resolve_file(files_root, kind, slug, rel)


def live_deploy_enabled() -> bool:
    if env_flag("AZOM_WP_DEPLOY_KILL"):
        return False
    return env_flag("AZOM_WP_LIVE_DEPLOY_ENABLED") and not is_mock_mode()


class DeployOrchestrator:
    """Orchestrates verified workspace deploys with ChangeLedger snapshots & auto-rollback."""

    def __init__(
        self,
        *,
        data_dir: Path | None = None,
        registry_path: Path | None = None,
    ) -> None:
        self.data_dir = data_dir_path(data_dir)
        self.registry_path = registry_path
        self.plans = ReviewPlans(data_dir=self.data_dir, registry_path=registry_path)
        self.ledger = ChangeLedger(data_dir=self.data_dir)

    def deploy_workspace(
        self,
        site_id: str,
        plan_id: str,
        *,
        actor: str,
        base_sha256: str,
        work_sha256: str,
        transport_mode: str = "auto",
        bridge_client: WpBridgeClient | None = None,
        deploy_transport: SshDeployTransport | None = None,
        health_check_fn: Any = None,
    ) -> WpOpsResult:
        if env_flag("AZOM_WP_DEPLOY_KILL"):
            return fail_result(
                "DeployKillSwitchActive",
                "Nödstopp (AZOM_WP_DEPLOY_KILL) är aktivt.",
                executed=False,
            )

        denied = self.plans._need(
            actor, Permission.WP_OPS_ADMIN, Permission.CODE_EDIT, action="wp_live_deploy"
        )
        if denied is not None:
            return denied

        if not is_mock_mode() and not live_deploy_enabled():
            return fail_result(
                "LiveDeployDisabled",
                "Live-deploy är avstängd (kräver AZOM_WP_LIVE_DEPLOY_ENABLED=1 i processmiljön).",
                executed=False,
                live_calls=False,
            )

        with self.plans._locks(site_id, plan_id):
            plan = self.plans._row(site_id, plan_id)
            if plan is None:
                return fail_result("UnknownPlan", "Okänd plan.", executed=False)
            blocked = self.plans._gate(plan, base_sha256, work_sha256)
            if blocked is not None:
                return blocked
            status_val = str(plan.get("status") or "")
            if status_val in _TERMINAL or plan.get("superseded_by"):
                return fail_result(
                    "PlanNotDeployable",
                    "Planen är ersatt eller avslutad och kan inte deployas.",
                    executed=False,
                    status=status_val,
                )
            if status_val not in {"tests_passed", "staged", "production_review"}:
                return fail_result(
                    "TestsNotPassed",
                    "Kontrollerna är inte godkända för deploy.",
                    executed=False,
                    plan_id=plan_id,
                    status=status_val,
                )
            evidence = self.plans._require_current_evidence(plan)
            if evidence is not None:
                return evidence
            if plan.get("risk") == "high":
                return fail_result("HighRiskBlocked", "Hög risk deployas inte automatiskt.", executed=False)
            site, missing = self.plans._site(site_id)
            if missing is not None or site is None:
                return missing or fail_result("UnknownSite", "Okänd webbplats.", executed=False)

            root = workspace_root(self.data_dir, site_id, plan_id)
            diff_ops = [
                entry for entry in _diff_ops(root) if entry[1] not in _SANDBOX_ONLY_FILES
            ]
            if not diff_ops:
                return fail_result("NoChange", "Inga deployerbara tema-/plugin-ändringar.", executed=False)

            # In mock mode with no explicit live transport injected, reuse staging LocalTree for staging,
            # or InMemory Bridge when transport_mode=="bridge".
            selected = self._select_transport(
                site,
                transport_mode=transport_mode,
                bridge_client=bridge_client,
                deploy_transport=deploy_transport,
            )
            if isinstance(selected, WpOpsResult):
                return selected
            mode, client_obj = selected

            kind = str(plan.get("kind") or "")
            slug = str(plan.get("slug") or "")
            file_entries: list[dict[str, Any]] = []
            before_files: dict[str, bytes] = {}
            after_files: dict[str, bytes] = {}
            expected_before_hashes: dict[str, str] = {}

            for op, rel, new_bytes, _in_base in diff_ops:
                mapped_op = "deleted" if op == "delete" else "updated"
                if op != "delete" and new_bytes is not None:
                    after_files[rel] = new_bytes
                base_file = root / "base" / rel
                if base_file.is_file():
                    raw_b = base_file.read_bytes()
                    before_files[rel] = raw_b
                    expected_before_hashes[rel] = sha256_digest(raw_b)
                elif op != "delete":
                    mapped_op = "created"
                    expected_before_hashes[rel] = sha256_digest(b"")
                file_entries.append({"path": rel, "op": mapped_op})

            if mode == "bridge":
                assert isinstance(client_obj, WpBridgeClient)
                change_row: dict[str, Any] | None = None
                try:
                    # Refresh live pre-write bytes via Bridge snapshot when present
                    snap_kind = (
                        "theme_file"
                        if kind == "theme"
                        else ("mu_plugin_file" if kind == "mu_plugin" else "plugin_file")
                    )
                    ledger_category = "mu_plugin_deploy" if kind == "mu_plugin" else "workspace_deploy"
                    for entry in file_entries:
                        rel = entry["path"]
                        snap = client_obj.get_snapshot(
                            kind=snap_kind,
                            slug=slug,
                            path=rel,
                        )
                        if snap.get("exists"):
                            if entry["op"] == "created":
                                return fail_result(
                                    "FileExistsConflict",
                                    f"Filen {rel} finns redan på live-sajten.",
                                    executed=False,
                                    live_calls=client_obj.is_live,
                                )
                            import base64

                            raw_b = base64.b64decode(str(snap.get("content") or ""))
                            live_sha = sha256_digest(raw_b)
                            base_sha = expected_before_hashes.get(rel, "")
                            if base_sha and live_sha != base_sha:
                                return fail_result(
                                    "StaleFile",
                                    f"Live-filen {rel} har ändrats sedan arbetsytan skapades (digest-avvikelse).",
                                    executed=False,
                                    live_calls=client_obj.is_live,
                                )
                            before_files[rel] = raw_b
                            expected_before_hashes[rel] = live_sha
                        else:
                            if client_obj.is_live and rel in before_files:
                                return fail_result(
                                    "StaleFile",
                                    f"Live-filen {rel} saknas men fanns i arbetsytans bas.",
                                    executed=False,
                                    live_calls=True,
                                )
                            before_files.pop(rel, None)
                            expected_before_hashes[rel] = sha256_digest(b"")
                            if entry["op"] != "deleted":
                                entry["op"] = "created"
                    change_row = self.ledger.record_change(
                        site_id=site.id,
                        environment=site.environment,
                        category=ledger_category,
                        transport="bridge",
                        actor=actor,
                        summary=str(plan.get("summary") or f"Bridge deploy {plan_id}"),
                        target_ref=f"{plan_id}:{kind}/{slug}",
                        file_entries=file_entries,
                        before_files=before_files,
                        after_files=after_files,
                        status="pending",
                        metadata={"plan_id": plan_id, "kind": kind, "slug": slug},
                    )
                    res = client_obj.apply_files(
                        change_id=change_row["change_id"],
                        kind=kind,
                        slug=slug,
                        entries=file_entries,
                        files=after_files,
                        expected_before_hashes=expected_before_hashes,
                    )
                    if health_check_fn is not None and not health_check_fn():
                        rb_entries = [
                            {
                                "path": e["path"],
                                "op": "deleted" if e["op"] == "created" else "updated",
                            }
                            for e in file_entries
                        ]
                        rb_ok = True
                        try:
                            client_obj.rollback(
                                change_id=change_row["change_id"],
                                kind=kind,
                                slug=slug,
                                entries=rb_entries,
                                files=before_files,
                            )
                        except Exception:
                            rb_ok = False
                        try:
                            self.ledger.mark_status(
                                site.id,
                                change_row["change_id"],
                                "rolled_back" if rb_ok else "failed",
                            )
                        except Exception:
                            pass
                        log_action(
                            actor=actor,
                            action="wp_live_deploy",
                            target="wp_sites",
                            target_id=site.id,
                            success=False,
                            details={
                                "plan_id": plan_id,
                                "change_id": change_row["change_id"],
                                "transport": "bridge",
                                "error": "PostDeployHealthFailed",
                                "auto_rolled_back": rb_ok,
                            },
                        )
                        return fail_result(
                            "PostDeployHealthFailed",
                            "Hälsokontroll efter Bridge-deploy misslyckades (automatisk återställning körd)."
                            if rb_ok
                            else "Hälsokontroll efter Bridge-deploy misslyckades och automatisk återställning fick fel.",
                            executed=False,
                            live_calls=client_obj.is_live,
                        )
                except Exception as exc:
                    if change_row is not None:
                        try:
                            self.ledger.mark_status(site.id, change_row["change_id"], "failed")
                        except Exception:
                            pass
                    log_action(
                        actor=actor,
                        action="wp_live_deploy",
                        target="wp_sites",
                        target_id=site.id,
                        success=False,
                        details={"plan_id": plan_id, "transport": "bridge", "error": str(exc)[:160]},
                    )
                    return fail_result(
                        "BridgeDeployFailed",
                        f"Bridge-deploy misslyckades (inga ändringar kvar live): {str(exc)[:160]}",
                        executed=False,
                        live_calls=client_obj.is_live,
                    )
                self.ledger.mark_status(site.id, change_row["change_id"], "applied")
                plan["status"] = "deployed"
                self.plans._replace(plan)
                write_manifest(root, plan)
                log_action(
                    actor=actor,
                    action="wp_live_deploy",
                    target="wp_sites",
                    target_id=site.id,
                    success=True,
                    details={
                        "plan_id": plan_id,
                        "change_id": change_row["change_id"],
                        "transport": "bridge",
                        "live_calls": client_obj.is_live,
                    },
                )
                return ok_result(
                    f"Deployad via Azom Ops Bridge (snapshot {change_row['change_id']}).",
                    plan_id=plan_id,
                    site_id=site.id,
                    change_id=change_row["change_id"],
                    transport="bridge",
                    executed=True,
                    live_calls=client_obj.is_live,
                    bridge_result=res,
                )

            # mode == "sftp_deploy"
            assert isinstance(client_obj, SshDeployTransport)
            sftp_changes: list[tuple[str, str, bytes | None]] = []
            sftp_expected_hashes: dict[str, str] = {}
            for entry in file_entries:
                rel = entry["path"]
                abs_path = _resolve_deploy_abs(client_obj.files_root, kind, slug, rel)
                if rel in expected_before_hashes:
                    sftp_expected_hashes[abs_path] = expected_before_hashes[rel]
                if entry["op"] == "deleted":
                    sftp_changes.append(("delete", abs_path, None))
                elif entry["op"] == "created":
                    sftp_changes.append(("create", abs_path, after_files[rel]))
                else:
                    sftp_changes.append(("write", abs_path, after_files[rel]))
            sftp_live = not is_mock_mode()
            try:
                bundle_res = client_obj.atomic_apply_bundle(
                    sftp_changes,
                    health_check_fn=health_check_fn,
                    expected_before_hashes=sftp_expected_hashes,
                )
            except Exception as exc:
                err_cls = str(getattr(exc, "error_class", "") or "")
                log_action(
                    actor=actor,
                    action="wp_live_deploy",
                    target="wp_sites",
                    target_id=site.id,
                    success=False,
                    details={"plan_id": plan_id, "transport": "sftp_deploy", "error": str(exc)[:160]},
                )
                if err_cls == "StaleFile":
                    return fail_result(
                        "StaleFile",
                        f"Målfilen har ändrats på servern sedan arbetsytan skapades: {str(exc)[:160]}",
                        executed=False,
                        live_calls=sftp_live,
                    )
                return fail_result(
                    "SftpDeployFailed",
                    f"Deploy SFTP misslyckades (automatisk återställning körd): {str(exc)[:160]}",
                    executed=False,
                    live_calls=sftp_live,
                )

            # Record actual pre-deploy remote bytes captured by SFTP atomic_apply_bundle
            remote_before: dict[str, bytes] = {}
            for entry in file_entries:
                rel = entry["path"]
                abs_path = _resolve_deploy_abs(client_obj.files_root, kind, slug, rel)
                if abs_path in bundle_res.get("before_bytes", {}):
                    remote_before[rel] = bundle_res["before_bytes"][abs_path]
                    if entry["op"] == "created":
                        entry["op"] = "updated"
                elif entry["op"] != "deleted":
                    entry["op"] = "created"
            ledger_warning = False
            change_id_val = ""
            try:
                change_row = self.ledger.record_change(
                    site_id=site.id,
                    environment=site.environment,
                    category="mu_plugin_deploy" if kind == "mu_plugin" else "workspace_deploy",
                    transport="sftp_deploy",
                    actor=actor,
                    summary=str(plan.get("summary") or f"SFTP deploy {plan_id}"),
                    target_ref=f"{plan_id}:{kind}/{slug}",
                    file_entries=file_entries,
                    before_files=remote_before,
                    after_files=after_files,
                    status="applied",
                    metadata={"plan_id": plan_id, "kind": kind, "slug": slug},
                )
                change_id_val = str(change_row["change_id"])
            except Exception:
                ledger_warning = True
            plan["status"] = "deployed"
            self.plans._replace(plan)
            write_manifest(root, plan)
            log_action(
                actor=actor,
                action="wp_live_deploy",
                target="wp_sites",
                target_id=site.id,
                success=True,
                details={
                    "plan_id": plan_id,
                    "change_id": change_id_val,
                    "transport": "sftp_deploy",
                    "live_calls": sftp_live,
                    "ledger_warning": ledger_warning,
                },
            )
            return ok_result(
                f"Deployad via *-wp-deployer SFTP (snapshot {change_id_val or 'varning'}).".strip(),
                plan_id=plan_id,
                site_id=site.id,
                change_id=change_id_val,
                transport="sftp_deploy",
                executed=True,
                live_calls=sftp_live,
                ledger_warning=ledger_warning,
            )

    def _select_transport(
        self,
        site: Any,
        *,
        transport_mode: str,
        bridge_client: WpBridgeClient | None,
        deploy_transport: SshDeployTransport | None,
    ) -> tuple[str, Any] | WpOpsResult:
        mode = str(transport_mode or "auto").strip().lower()
        if mode in {"auto", "bridge"}:
            bc = bridge_client or (
                bridge_client_for_site(site) if (is_mock_mode() or bridge_enabled()) else None
            )
            if bc is not None:
                return "bridge", bc
            if mode == "bridge":
                return fail_result(
                    "BridgeUnconfigured",
                    "Azom Ops Bridge är inte aktiverad eller saknar WP_BRIDGE_SECRET_<SITE>.",
                    executed=False,
                )
        if mode in {"auto", "sftp_deploy"}:
            if deploy_transport is not None:
                return "sftp_deploy", deploy_transport
            if deploy_ssh_enabled():
                return "sftp_deploy", SshDeployTransport(site, self.data_dir)
            if mode == "sftp_deploy":
                return fail_result(
                    "DeploySshUnconfigured",
                    "Deploy SFTP är inte aktiverad (AZOM_WP_DEPLOY_SSH=1).",
                    executed=False,
                )
        return fail_result(
            "DeployTransportUnavailable",
            "Ingen godkänd deploy-transport (Bridge eller *-wp-deployer SFTP) är konfigurerad.",
            executed=False,
        )


__all__ = ["DeployOrchestrator", "live_deploy_enabled"]
