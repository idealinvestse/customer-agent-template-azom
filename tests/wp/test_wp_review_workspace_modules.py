"""Unit tests for PR 4.2 split review workspace modules and facade."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from ecom_ops.wp_ops import review_workspace
from ecom_ops.wp_ops.file_transport import LocalTree
from ecom_ops.wp_ops.review import (
    component_reader,
    scaffolds,
    workspace_io,
    workspace_ops,
)


def test_review_workspace_facade_preserves_object_identity() -> None:
    assert review_workspace.ReviewError is workspace_io.ReviewError
    assert review_workspace.VerifiedTheme is workspace_io.VerifiedTheme
    assert review_workspace.VerifiedPlugin is workspace_io.VerifiedPlugin
    assert review_workspace.workspace_root is workspace_io.workspace_root
    assert review_workspace.tree_hash is workspace_io.tree_hash
    assert review_workspace.unified_diff_text is workspace_io.unified_diff_text

    assert review_workspace.read_local_component is component_reader.read_local_component
    assert review_workspace.read_site_component is component_reader.read_site_component
    assert review_workspace.read_theme_woo_files is component_reader.read_theme_woo_files

    assert review_workspace.patch_work_file is workspace_ops.patch_work_file
    assert review_workspace.create_work_file is workspace_ops.create_work_file
    assert review_workspace.apply_work_changes is workspace_ops.apply_work_changes
    assert review_workspace.bulk_replace_work is workspace_ops.bulk_replace_work
    assert review_workspace.theme_json_work is workspace_ops.theme_json_work

    assert review_workspace.child_skeleton is scaffolds.child_skeleton
    assert review_workspace.plugin_skeleton is scaffolds.plugin_skeleton
    assert review_workspace.scaffold_work is scaffolds.scaffold_work


def test_component_reader_unified_walker_local_and_remote(tmp_path: Path) -> None:
    # LocalTree path
    theme_root = tmp_path / "wp-content" / "themes" / "azom-child"
    (theme_root / "woocommerce").mkdir(parents=True)
    (theme_root / "wc-custom.php").write_bytes(b"<?php // root\n")
    (theme_root / "woocommerce" / "single-product.php").write_bytes(b"<?php // woo\n")
    (theme_root / "woocommerce" / ".env").write_bytes(b"SECRET=1\n")

    local = LocalTree("/srv/www/shop", tmp_path)
    woo_local = component_reader.read_theme_woo_files(local, "/srv/www/shop", "azom-child")
    assert woo_local["has_woocommerce_dir"] is True
    assert woo_local["root_wc_templates"] == ["wc-custom.php"]
    assert woo_local["woo_files"] == [("woocommerce/single-product.php", "<?php // woo\n")]

    # Remote FileTransport path (uses open_batch + list_entries)
    class _FakeRemoteTransport:
        is_ssh = True

        def __init__(self) -> None:
            self.batched = False

        @contextmanager
        def open_batch(self):
            self.batched = True
            yield self

        def list_entries(self, abs_dir: str) -> list[tuple[str, bool]]:
            if abs_dir.endswith("/themes/azom-child"):
                return [("style.css", False), ("wc-card.php", False), ("woocommerce", True)]
            if abs_dir.endswith("/woocommerce"):
                return [("cart.php", False)]
            return []

        def read_bytes(self, abs_file: str) -> bytes:
            if abs_file.endswith("/style.css"):
                return b"body { color: #000; }\n"
            if abs_file.endswith("/wc-card.php"):
                return b"<?php // card\n"
            if abs_file.endswith("/woocommerce/cart.php"):
                return b"<?php // cart\n"
            raise FileNotFoundError(abs_file)

    remote = _FakeRemoteTransport()
    files = component_reader.read_site_component(remote, "/srv/www/shop", "theme", "azom-child")
    assert remote.batched is True
    assert [rel for rel, _data in files] == ["style.css", "wc-card.php", "woocommerce/cart.php"]

    woo_remote = component_reader.read_theme_woo_files(remote, "/srv/www/shop", "azom-child")
    assert woo_remote["has_woocommerce_dir"] is True
    assert woo_remote["root_wc_templates"] == ["wc-card.php"]
    assert woo_remote["woo_files"] == [("woocommerce/cart.php", "<?php // cart\n")]


def test_table_driven_scaffolds_and_unified_diff_filters_secrets(tmp_path: Path) -> None:
    ws = tmp_path / "wr-0123456789ab"
    (ws / "base").mkdir(parents=True)
    (ws / "work").mkdir(parents=True)

    child_res = scaffolds.scaffold_work(ws, "child_theme_basics", "storefront-child")
    assert child_res["created"] == ["style.css", "functions.php"]
    assert "Template: storefront" in (ws / "work" / "style.css").read_text(encoding="utf-8")

    # Second call skips existing files idempotently
    child_again = scaffolds.scaffold_work(ws, "child_theme_basics", "storefront-child")
    assert child_again["created"] == []
    assert child_again["skipped"] == ["style.css", "functions.php"]

    woo_res = scaffolds.scaffold_work(ws, "woo_compat_declaration", "azom-helper")
    assert "includes/woo-hpos-compat.php" in woo_res["created"]
    assert "azom-helper.php" in woo_res["created"]

    with pytest.raises(workspace_io.ReviewError):
        scaffolds.scaffold_work(ws, "unknown_template")

    diff = workspace_io.unified_diff_text(
        {".env": b"OLD=1\n", "style.css": b"a {}\n"},
        {".env": b"SECRET=2\n", "style.css": b"a { color: red; }\n"},
    )
    assert "SECRET=2" not in diff
    assert "color: red" in diff
