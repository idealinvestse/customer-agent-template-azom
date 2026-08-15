"""MailClient.send/reply stay inside actions/mail.py (plus the MailClient itself)."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "skills" / "ecom_ops"
ALLOWED = {
    ROOT / "actions" / "mail.py",
    ROOT / "integrations" / "mail.py",
}


def test_mail_client_send_reply_not_called_outside_mail_action():
    offenders: list[str] = []
    for path in ROOT.rglob("*.py"):
        if path in ALLOWED:
            continue
        text = path.read_text(encoding="utf-8")
        if "client.send(" in text or "client.reply(" in text:
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
