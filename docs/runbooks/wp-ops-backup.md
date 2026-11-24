# Runbook: WordPress mock-backup och staging-drill

**Purpose:** Skapa ett lokalt mock-manifest och registrera en staging-drill utan att kopiera sajtfiler eller återställa produktion.  
**Audience:** Oscar och den som kör agenten med rollen operator. Jonatan kan läsa listan.  
**Read this first:** [`../WORDPRESS_OPS.md`](../WORDPRESS_OPS.md), [`../CURRENT_STATE.md`](../CURRENT_STATE.md), [`../PILOT_OPS.md`](../PILOT_OPS.md).

## Ordlista

| Term | Betydelse |
|------|-----------|
| **Mock-manifest** | Metadata i `wp_ops_state.json`. Ett id täcker etiketterna `files` och `database`. Inga bytes kopieras. |
| **Off-site** | Fältet `offsite` på mock-manifestet är alltid falskt. `bin/backup-azom-data.sh` kan ändå rsynca `AZOM_BACKUP_DIR` när `--dest` eller `AZOM_BACKUP_DEST` är satt. Den tarbollen kan innehålla `wp_ops_state.json` och `wp_review_plans.json` och utesluter `wp_workspaces/`. Det är inte en sajtåterställning. |
| **Kryptering** | `unconfigured`. Digesten är SHA-256 av metadata, inte en nyckel. |
| **recovery_seconds** | Tid för den lokala kontrollen. Det är inte kundens återställningstid. |
| **Produktionsåterställning** | `wp restore` finns inte. `restore()` svarar `RestoreNotInMvp` och kör inget. |

## Gör / gör inte

**Gör:**

- Kör med `AZOM_USE_MOCK=1` tills Oscar har godkänt en namngiven pilot.
- Använd sajterna `mock-shop` och `mock-staging` under `example.test`.
- Låt en verifierad mock-backup stå som “verifierad, inte off-site”. Den är inte grön.
- Behåll posten när retention (standard 14 dagar) har gått ut. Den är då inte en giltig backup.

**Gör inte:**

- Skicka backup till en tredje part eller hitta på en lagringsleverantör.
- Generera en krypteringsnyckel eller klistra in ett applösenord i registret, planen eller audit.
- Rikta drillen mot `production`.
- Markera live soak, managed hosting eller en tillgänglighets-SLA som klart.

## 1. Skapa manifest

```bash
# cwd: repository root
# env: AZOM_USE_MOCK=1
python -m ecom_ops --mock --actor oscar wp backup --id mock-shop
# expect: ok true, live_calls false, encryption unconfigured, offsite false, bytes_copied false
```

Staging kräver `WP_OPS_OPERATE`. Produktion kräver `WP_OPS_ADMIN`. Operator får avslag på `mock-shop`.

## 2. Läs listan

```bash
python -m ecom_ops --mock --actor jonatan wp backups --id mock-shop
# expect: ok true och samma backup-id. Hemlighetsvärdet syns inte.
```

## 3. Staging-drill

```bash
python -m ecom_ops --mock --actor agent wp restore-drill --backup <backup-id> --target mock-staging
# expect: ok true, target_environment staging, parts files och database, orders_mutated false
```

En WooCommerce-källa får `order_reconciliation: required`. Inga ordrar ändras. Samma backup och mål inom dedupe-fönstret returnerar den förra drillen.

## 4. Dashboard

1. Öppna `/wp` och sedan sajten.
2. Oscar: “Skapa mock-backup” på produktion. Operator-behörighet räcker på staging.
3. “Kör staging-drill” visar bara staging-mål.
4. Bekräfta att badgen inte är grön och att sidan inte har “Återställ till produktion”.

## 5. Verifiera att produktion fortfarande vägrar

`wp restore` är ett okänt kommando och avslutas med kod 2. Ett drill-mål i `production` får `TargetNotStaging` och `executed: false`.
