# Local identity realm

`threshold-realm.json` is imported on startup by `--import-realm`, so the roles
and clients are reproducible from version control rather than clicked into
existence in an admin console.

The file carries no comments because Keycloak's `RealmRepresentation` rejects
unknown fields outright — a `_comment` key fails the import with
`Unrecognized field`, and the container exits. The explanation lives here.

## The four roles are domain concepts

They are not generic admin/user tiers. Owner routing in M3 maps each exception
class to exactly one of these, so the taxonomy and the authorisation model are
the same model:

| Role | Owns |
|---|---|
| `ap_clerk` | Routine exception review. Cannot approve above the delegation limit. |
| `buyer` | Price and quantity variances against the purchase order they raised. |
| `controller` | Vendor banking changes and anything above the delegation limit. |
| `auditor` | Read-only across every projection, including the audit trail. |

A vendor banking change routing to `controller` rather than to whoever is on
shift is the control that matters most here: it is the shape almost every real
accounts-payable fraud takes.

## The two clients

- **`threshold-api`** — bearer-only and confidential. It validates tokens; it
  never initiates a login.
- **`threshold-console`** — public client, authorization code with PKCE, no
  client secret. A secret shipped to a browser is not a secret, and the
  alternative to PKCE here would be the implicit flow, which is deprecated for
  good reason.

## The three users are local-only

`clerk`, `controller` and `auditor` exist so the two-projection demo can be
shown without an admin console. They are defined in a file that is only mounted
by `docker-compose.yml`, which is a local development artifact.

This is also the reason Keycloak is here at all rather than a hand-rolled JWT
helper. There is a prior repository in this portfolio with
`sha256("prefix:" + password)` and a default secret of
`"arena-dev-secret-change-me"` committed to it. Delegating authentication to
something that implements the specification removes that entire class of
mistake rather than patching one instance of it.
