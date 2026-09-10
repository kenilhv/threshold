#!/usr/bin/env bash
# Proves a running stack is actually serving, not merely "up".
#
# `docker compose ps` reporting healthy is a weaker claim than this: it means
# each container passed its own healthcheck, not that the system works. This
# checks the things a stranger would check in the first ninety seconds.
set -euo pipefail

API="${API:-http://localhost:8000}"
fail=0

# Counted rather than asserted in prose. A number written into the README
# drifts the first time a check is added, and a stale count in documentation
# is the same class of defect this script exists to catch.
checks=0

check() {
  local name="$1" expected="$2" actual="$3"
  checks=$((checks + 1))
  if [[ "$actual" == "$expected" ]]; then
    printf '  \033[32mPASS\033[0m  %-42s %s\n' "$name" "$actual"
  else
    printf '  \033[31mFAIL\033[0m  %-42s got %s, expected %s\n' "$name" "$actual" "$expected"
    fail=1
  fi
}

echo
echo "Threshold smoke test"
echo "===================="

check "GET /healthz" "200" \
  "$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$API/healthz")"

check "GET /readyz" "200" \
  "$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$API/readyz")"

check "readiness reports every dependency up" "true" \
  "$(curl -s --max-time 10 "$API/readyz" | python -c 'import json,sys; print(str(json.load(sys.stdin)["ready"]).lower())')"

check "OpenAPI contract is served" "200" \
  "$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$API/openapi.json")"

check "request id is echoed" "smoke-test-id" \
  "$(curl -s -D - -o /dev/null --max-time 10 -H 'X-Request-Id: smoke-test-id' "$API/healthz" \
     | tr -d '\r' | awk -F': ' 'tolower($1)=="x-request-id"{print $2}')"

# Migrations actually applied, rather than the container merely having exited 0.
check "schema is migrated (5 tables)" "5" \
  "$(docker compose exec -T postgres psql -U threshold -d threshold -tAc \
     "select count(*) from information_schema.tables where table_schema='public' and table_name in ('vendor','vendor_bank_account','audit_event','money_example','alembic_version')")"

check "seed loaded 5 vendors" "5" \
  "$(docker compose exec -T postgres psql -U threshold -d threshold -tAc 'select count(*) from vendor')"

check "bank history kept (4 versions)" "4" \
  "$(docker compose exec -T postgres psql -U threshold -d threshold -tAc 'select count(*) from vendor_bank_account')"

# The invariant that makes the audit trail trustworthy. If this ever returns
# UPDATE-succeeded, the append-only guarantee is decoration.
check "audit_event rejects UPDATE" "blocked" \
  "$(docker compose exec -T postgres psql -U threshold -d threshold -tAc \
     "insert into audit_event (actor_type, action, entity_type, entity_id) values ('system','smoke.test','vendor', gen_random_uuid());
      do \$\$ begin update audit_event set action='tampered' where action='smoke.test'; raise notice 'ALLOWED'; exception when others then raise notice 'blocked'; end \$\$;" 2>&1 \
     | grep -o 'blocked' | head -1)"

check "seed loaded 5 purchase orders" "5" \
  "$(docker compose exec -T postgres psql -U threshold -d threshold -tAc 'select count(*) from purchase_order' | tr -d '\r' | head -1)"


check "pgvector extension present" "vector" \
  "$(docker compose exec -T postgres psql -U threshold -d threshold -tAc \
     "select extname from pg_extension where extname='vector'")"

check "MinIO bucket exists" "200" \
  "$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://localhost:9000/minio/health/live)"

# ---------------------------------------------------------------- ingest ----
# Generates a fixture invoice, submits it, then submits the identical bytes
# again. The second submission must not create a second document.

# errexit is disabled around fixture generation: it needs the dev extras
# installed, which a reviewer running only `make up` will not have. A missing
# fixture must skip these checks, never abort the whole smoke test.
FIXTURE=".smoke-invoice.pdf"
set +e
python - "$FIXTURE" <<'PY' >/dev/null 2>&1
import dataclasses, sys, uuid
from pathlib import Path
from tests.unit.test_fixture_layouts import build_spec, render

# The invoice number is varied, not just the seed. Fixtures are deliberately
# deterministic, so a fresh seed alone can still render byte-identical output —
# in which case the system correctly deduplicates it and "created" is false.
# That is right behaviour and a useless smoke check, so make the bytes genuinely
# new by changing something that reaches the page.
spec = build_spec()
spec = dataclasses.replace(spec, invoice_number=f"SMOKE-{uuid.uuid4().hex[:10].upper()}")
render("continental_tabular", spec, Path(sys.argv[1]))
PY
set -e

if [[ -s "$FIXTURE" ]]; then
  FIRST="$(curl -s --max-time 30 -X POST "$API/v1/documents" -F "file=@$FIXTURE;type=application/pdf")"
  check "POST /v1/documents creates a document" "true" \
    "$(printf '%s' "$FIRST" | python -c 'import json,sys; print(str(json.load(sys.stdin)["created"]).lower())' 2>/dev/null)"

  # The POST must hand back a job id, not a finished extraction. If this ever
  # reads false, the API went back to doing the work on the request thread.
  check "work was queued rather than done inline" "true" \
    "$(printf '%s' "$FIRST" | python -c 'import json,sys; print("true" if json.load(sys.stdin).get("job_id") else "false")' 2>/dev/null)"

  SECOND="$(curl -s --max-time 30 -X POST "$API/v1/documents" -F "file=@$FIXTURE;type=application/pdf")"
  check "identical bytes do not create a second document" "false" \
    "$(printf '%s' "$SECOND" | python -c 'import json,sys; print(str(json.load(sys.stdin)["created"]).lower())' 2>/dev/null)"

  DOC_ID="$(printf '%s' "$FIRST" | python -c 'import json,sys; print(json.load(sys.stdin)["document_id"])' 2>/dev/null)"
  check "GET /v1/documents/{id} returns the extraction" "200" \
    "$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$API/v1/documents/$DOC_ID")"

  # Reusing a key for different content must be refused, not silently replayed.
  KEY="smoke-$(date +%s)-$$"
  curl -s -o /dev/null --max-time 30 -X POST "$API/v1/documents" \
    -H "Idempotency-Key: $KEY" -F "file=@$FIXTURE;type=application/pdf"
  check "reusing an Idempotency-Key for other content is a 409" "409" \
    "$(printf 'different bytes entirely' > "$FIXTURE.other"; \
       curl -s -o /dev/null -w '%{http_code}' --max-time 30 -X POST "$API/v1/documents" \
         -H "Idempotency-Key: $KEY" -F "file=@$FIXTURE.other;type=application/pdf")"
  rm -f "$FIXTURE.other"

  # Extraction is asynchronous now, so the smoke test has to wait for the
  # worker rather than assume the response already carries a result.
  EXTRACTED=""
  for _ in $(seq 1 40); do
    EXTRACTED="$(curl -s --max-time 15 "$API/v1/documents/$DOC_ID" \
      | python -c 'import json,sys; print(json.load(sys.stdin).get("extraction_version") or "")' 2>/dev/null)"
    [[ -n "$EXTRACTED" ]] && break
    sleep 0.5
  done
  check "the worker extracts the document" "1" "$EXTRACTED"

  # A claimed job whose lease has already expired is work nobody is doing and
  # nobody has noticed. The reaper exists so this count stays zero.
  check "no job is stuck holding a dead lease" "0" \
    "$(docker compose exec -T postgres psql -U threshold -d threshold -tAc \
       "select count(*) from job where state='claimed' and lease_expires_at < now()" | tr -d '\r' | head -1)"

  # Two jobs now, not one: extraction enqueues the match in its own transaction.
  # This counting 1 again would mean the chain broke.
  check "both of this document's jobs reached a terminal state" "2" \
    "$(docker compose exec -T postgres psql -U threshold -d threshold -tAc \
       "select count(*) from job where payload->>'document_id' = '$DOC_ID' and state in ('succeeded','dead')" | tr -d '\r' | head -1)"

  # --- the decision layer -------------------------------------------------
  # Extraction finishing is no longer the end of the pipeline. The seeded order
  # PO-44718 is the one this fixture cites, and its lines carry the same SKUs
  # and the same agreed price, so a clean fixture must come out auto_approve.
  # If this ever reads needs_review, either the extractor stopped reading line
  # items or the seed and the fixture have drifted apart.
  DECISION=""
  for _ in $(seq 1 40); do
    DECISION="$(curl -s --max-time 15 "$API/v1/documents/$DOC_ID/match" \
      | python -c 'import json,sys; print(json.load(sys.stdin).get("disposition") or "")' 2>/dev/null)"
    [[ -n "$DECISION" ]] && break
    sleep 0.5
  done
  check "the invoice was matched against its purchase order" "auto_approve" "$DECISION"

  MATCH="$(curl -s --max-time 15 "$API/v1/documents/$DOC_ID/match")"

  check "all three line items paired with the order" "3" \
    "$(printf '%s' "$MATCH" | python -c 'import json,sys; print(len(json.load(sys.stdin)["line_pairs"]))' 2>/dev/null)"

  # A clean result only means something if every check actually ran. An absent
  # check and a passing check look identical from the outside.
  check "no check was skipped on the clean path" "0" \
    "$(printf '%s' "$MATCH" | python -c 'import json,sys; print(len(json.load(sys.stdin)["skipped_checks"]))' 2>/dev/null)"

  check "the decision names the policy it applied" "2026.09.1" \
    "$(printf '%s' "$MATCH" | python -c 'import json,sys; print(json.load(sys.stdin)["policy_version"])' 2>/dev/null)"



else
  printf '  \033[33mSKIP\033[0m  ingest checks (could not build a fixture)\n'
fi
rm -f "$FIXTURE"

echo
if [[ $fail -eq 0 ]]; then
  printf '[32mAll %d checks passed.[0m

' "$checks"
else
  printf '\033[31mSmoke test failed.\033[0m\n\n'
  exit 1
fi
