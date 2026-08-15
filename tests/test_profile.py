"""Customer profile defaults and fail-closed independence."""

from __future__ import annotations

from ecom_ops.profile import load_profile, reload_profile  # noqa: F401


def test_missing_profile_uses_azom_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(tmp_path))
    p = load_profile()
    assert p.customer == "azom"
    assert p.viewer_actor == "jonatan"
    assert p.admin_actor == "oscar"
    assert p.actor_usernames() == ("jonatan", "oscar")


def test_profile_yaml_loads(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(tmp_path))
    (tmp_path / "profile.yaml").write_text(
        "customer: demo\nbrand: Demo\nactors:\n  viewer: anna\n  admin: bo\n",
        encoding="utf-8",
    )
    p = load_profile()
    assert p.customer == "demo"
    assert p.viewer_actor == "anna"
    assert p.admin_actor == "bo"


def test_cli_defaults_follow_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("AZOM_CONFIG_DIR", str(tmp_path))
    (tmp_path / "profile.yaml").write_text(
        "customer: demo\nactors:\n  viewer: anna\n  admin: bo\n  operator: agent\n",
        encoding="utf-8",
    )
    from ecom_ops.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["version"])
    assert args.site == "demo"


def test_empty_profile_does_not_weaken_fail_closed(monkeypatch):
    """Profile is display/config only — live empty allowlists still deny."""
    from ecom_ops.bot.actors import channel_peer_allowed

    monkeypatch.setenv("AZOM_USE_MOCK", "0")
    monkeypatch.delenv("MESSENGER_ALLOWED_PSIDS", raising=False)
    assert channel_peer_allowed("messenger", "123") is False
