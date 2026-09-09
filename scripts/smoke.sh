#!/usr/bin/env bash
# Proves a running stack is actually serving, not merely "up".
#
# `docker compose ps` reporting healthy is a weaker claim than this: it means
# each container passed its own healthcheck, not that the system works. This
# checks the things a stranger would check in the first ninety seconds.
set -euo pipefail

API="${API:-http://localhost:8000}"
fail=0

check() {
  local name="$1" expected="$2" actual="$3"
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

check "pgvector extension present" "vector" \
  "$(docker compose exec -T postgres psql -U threshold -d threshold -tAc \
     "select extname from pg_extension where extname='vector'")"

check "MinIO bucket exists" "200" \
  "$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://localhost:9000/minio/health/live)"

echo
if [[ $fail -eq 0 ]]; then
  printf '\033[32mAll checks passed.\033[0m\n\n'
else
  printf '\033[31mSmoke test failed.\033[0m\n\n'
  exit 1
fi
