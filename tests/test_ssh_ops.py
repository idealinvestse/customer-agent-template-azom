"""SSH ops + escalation tests."""

from ecom_ops.actions.ssh_ops import SSHOpsService
from ecom_ops.integrations.ssh import LocalMockSSHRunner, SSHClient


def test_allowlisted_ssh(ssh_client, telemetry, escalation):
    svc = SSHOpsService(client=ssh_client, telemetry=telemetry, escalation=escalation)
    result = svc.run("uptime", actor="agent")
    assert result.ok
    assert result.result is not None
    assert result.result.exit_code == 0
    assert not result.escalated


def test_destructive_escalates(ssh_client, telemetry, escalation):
    svc = SSHOpsService(client=ssh_client, telemetry=telemetry, escalation=escalation)
    result = svc.run("rm -rf /var/www", actor="agent")
    assert not result.ok
    assert result.escalated
    assert result.ticket_id


def test_code_edit_escalates(ssh_client, telemetry, escalation):
    svc = SSHOpsService(client=ssh_client, telemetry=telemetry, escalation=escalation)
    result = svc.run("sed -i 's/foo/bar/' wp-config.php", actor="oscar")
    assert result.escalated
    assert result.ticket_id


def test_metacharacters_blocked(ssh_client, telemetry, escalation):
    svc = SSHOpsService(client=ssh_client, telemetry=telemetry, escalation=escalation)
    result = svc.run("uptime; rm -rf /", actor="agent")
    assert not result.ok
    # SecurityError path (no escalate) or escalate — either is safe
    assert result.escalated or "not allowed" in result.message or "metacharacter" in result.message.lower() or "chaining" in result.message.lower()


def test_health_checks(ssh_client, telemetry, escalation):
    svc = SSHOpsService(client=ssh_client, telemetry=telemetry, escalation=escalation)
    results = svc.health(actor="agent")
    assert len(results) == 4
    assert all(r.ok for r in results)


def test_jonatan_can_read_ssh():
    client = SSHClient(host="h", runner=LocalMockSSHRunner())
    # viewer has SSH_READ
    from ecom_ops.escalation import EscalationService
    from ecom_ops.telemetry import Telemetry
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        svc = SSHOpsService(
            client=client,
            telemetry=Telemetry(path=Path(d) / "t.jsonl"),
            escalation=EscalationService(store_path=Path(d) / "e.jsonl", notifiers=[]),
        )
        result = svc.run("hostname", actor="jonatan")
        assert result.ok
