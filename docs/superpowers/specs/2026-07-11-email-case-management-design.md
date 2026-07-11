# Design: Semi-automated email case management (MVP)

**Date:** 2026-07-11  
**Status:** approved for implementation (Approach 1)

## Locked decisions

| Decision | Choice |
|----------|--------|
| MVP primary | Incoming mail → auto-created cases in DB |
| After create | Draft reply suggested; send requires approval |
| Storage | SQLite under `AZOM_DATA_DIR/cases.db` |
| Mailboxes | Configurable N (YAML) |
| Approve/send | Jonatan + Oscar + agent (CASE_REPLY permission for viewer) |
| Out of MVP | FAQ/knowledge base, auto-send without approval |

## Flow

1. Poller loads mailboxes from `config/mailboxes.yaml`
2. Fetch unread (or recent) per mailbox via existing MailClient
3. Dedupe on `message_id`; create case + inbound message; run SupportService draft
4. Dashboard `/cases` queue; detail page approve → MailService.send; status=closed/replied
5. Telegram `/cases` lists open cases

## Non-goals (later)

- Knowledge base / FAQ learning
- Multi-tenant SaaS
- Real-time IMAP IDLE
