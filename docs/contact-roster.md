# Private contact rosters

Issue [#20](https://github.com/organvm/hospes/issues/20) adds a private,
tenant/show-scoped roster for four route roles: `publicist`, `manager`, `agent`,
and `direct`.

## Import boundary

`spec/candidate.schema.json` is the versioned **private-input-only** contract.
The CSV `contact_roster` column contains one JSON object. Each role may carry
`name`, `email`, `phone`, `notes`, an opaque `provenance_ref`, `verified_at`,
`usable`, `preferred`, and `permission_status`.

```json
{
  "publicist": {
    "name": "Synthetic Publicist",
    "email": "publicist@example.test",
    "phone": "+1 555 010 0200",
    "notes": "Synthetic test-only route.",
    "provenance_ref": "roster://fixture/publicist-1",
    "verified_at": "2026-08-10T12:00:00+00:00",
    "usable": true,
    "preferred": true,
    "permission_status": "permitted"
  }
}
```

Set `HOSPES_MASTER_KEY_B64` from the credential-wall master-key atom before an
import containing a roster. The key must decode to exactly 32 bytes. The CLI
never prints contact values:

```bash
python3 -m hospes import-candidates /private/path/candidates.csv \
  --tenant private_pilot --network ari_network --show flagship_private_pilot \
  --actor anthony_operator --role producer
```

The complete batch is validated before mutation. HOSPES then encrypts every
name, email, phone, and note independently with AES-256-GCM. Authenticated
additional data binds tenant, show, `contact_rosters`, roster record, field,
category, and tenant key version. The database retains only ciphertext and
`private-field://` references. An exact re-import is write-free; an `opted_out`
route cannot be reactivated by import.

## Read boundary

`GET /v1/opportunities/{id}` and `GET /v1/approval-queue` always include only
`contact_roster_refs` in their public projection. When the API has a field vault
and the authenticated role is `host`, `producer`, `editorial_owner`, or
`relationship_owner`, the approval queue may add a transient `contact_roster`
view and the dedicated endpoint returns:

- the role-gated transient roster values; and
- one invitation prefill selected from a verified, permitted, usable route.

`network_operator`, portal, and other roles cannot reveal roster values. The
dashboard escapes every rendered value, hides contact surfaces from unauthorized
or read-only sessions, and provides no send control. Prefill is display-only: it
does not book, send, or persist the private route in correspondence metadata.

## Predicate

```bash
python -m pytest tests/issue_predicates/test_issue_20.py -q
```

This predicate covers schemas and packaged-resource parity, migration and
foreign-key scope, encryption/AAD isolation, idempotency, rollback, role and
tenant denial, API projections, workbench prefill, documentation, and private
data scanning.
