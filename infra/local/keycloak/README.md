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
| `ap_clerk` | Routine exception review: document arithmetic, duplicates, unresolved vendors. |
| `buyer` | Price and quantity variances against the purchase order they raised. |
| `controller` | Financial control. Owns no exception class in the shipped taxonomy. |
| `auditor` | Read-only across every projection, including the audit trail. |

> **This realm has drifted from the domain, and nothing yet consumes it.**
>
> `domain/taxonomy.py` routes exceptions to four owners — `ap_clerk`, `buyer`,
> `receiving` and `treasury` — and two of those are not realm roles at all. A
> vendor banking change routes to `treasury`, not to `controller`, which the
> table above used to claim. No seeded user holds `buyer`. The realm defines no
> protocol mappers, so role delivery rests on a Keycloak default and the access
> token's audience is `account` rather than `threshold-api`.
>
> None of this has bitten, because **no endpoint is authenticated yet**: the
> API reads `oidc_issuer` from settings and uses it nowhere. That is the gap
> M4 closes, and closing it means reconciling this file with the taxonomy
> rather than the other way round — renaming an `OwnerRole` would rewrite the
> owner stamped on every exception already stored.
>
> An earlier version of this table also described a delegation limit. No such
> limit exists anywhere in the code, so the sentence has been removed rather
> than left as a specification nobody wrote.

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
