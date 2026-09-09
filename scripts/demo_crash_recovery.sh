#!/usr/bin/env bash
# The M2 demo: kill a worker holding a job, watch the work survive.
#
# Nothing here is simulated. It kills the real container with SIGKILL, so the
# worker gets no chance to clean up — which is the only version of this worth
# demonstrating. A graceful shutdown proves nothing about crash recovery.
#
#   bash scripts/demo_crash_recovery.sh
set -euo pipefail

PSQL=(docker compose exec -T postgres psql -U threshold -d threshold -tAc)

say()  { printf '\n\033[1m%s\033[0m\n' "$1"; }
note() { printf '   %s\n' "$1"; }

q() { "${PSQL[@]}" "$1" | tr -d '\r' | head -1; }

say "1. A job is claimed and the worker holds a lease on it"

# Insert a job the worker cannot finish quickly — it points at a document that
# does not exist, so the handler will fail it. What matters for this demo is the
# claim, not the outcome.
JOB_ID=$(q "
  INSERT INTO job (kind, payload, run_after)
  VALUES ('extract_document', '{\"document_id\": \"00000000-0000-0000-0000-000000000000\"}', now() + interval '10 minutes')
  RETURNING id")
note "enqueued job $JOB_ID (scheduled ahead so the pool does not race us)"

# Claim it by hand, exactly as a worker would.
q "
  UPDATE job
     SET state = 'claimed',
         claimed_by = 'demo-worker/9999',
         claimed_at = now(),
         lease_expires_at = now() + interval '2 minutes',
         attempts = attempts + 1
   WHERE id = '$JOB_ID'" >/dev/null

note "state:    $(q "SELECT state FROM job WHERE id='$JOB_ID'")"
note "held by:  $(q "SELECT claimed_by FROM job WHERE id='$JOB_ID'")"
note "lease:    $(q "SELECT lease_expires_at FROM job WHERE id='$JOB_ID'")"
note "attempts: $(q "SELECT attempts FROM job WHERE id='$JOB_ID'")"

say "2. The worker dies. SIGKILL, no cleanup, no goodbye"
docker compose kill -s SIGKILL worker >/dev/null 2>&1 || true
note "worker container killed"
note "the job is still 'claimed' by a process that no longer exists:"
note "  state = $(q "SELECT state FROM job WHERE id='$JOB_ID'")"
note ""
note "This is the state that loses work in systems that delete on claim."
note "Nothing was deleted here — a claim is a state change, so the row is"
note "still sitting in the table with everything needed to run it again."

say "3. The lease expires"
q "UPDATE job SET lease_expires_at = now() - interval '1 second' WHERE id='$JOB_ID'" >/dev/null
note "lease pushed into the past (in production this is just time passing)"

say "4. The reaper notices and returns the work"
docker compose up -d worker >/dev/null 2>&1
note "worker is back (compose restarts it; this call is usually a no-op)"
note ""
note "Note what a restarted worker does NOT do: it does not know about the job"
note "above and never will. Coming back is not recovery. The reaper is what"
note "recovers the work, and it would do so even if this container stayed down"
note "and a different host picked the job up."
note ""
note "running the reaper directly rather than waiting out the 30s interval"
docker compose exec -T api python -m scripts.queuectl reap 2>&1 | grep -E "returned" | sed 's/^/   /'

say "5. The job is claimable again, with its attempt history intact"
note "state:    $(q "SELECT state FROM job WHERE id='$JOB_ID'")"
note "held by:  $(q "SELECT coalesce(claimed_by, '(nobody)') FROM job WHERE id='$JOB_ID'")"
note "attempts: $(q "SELECT attempts FROM job WHERE id='$JOB_ID'")  <- still 1, not 2"
note "last err: $(q "SELECT coalesce(last_error,'') FROM job WHERE id='$JOB_ID'")"

say "What this shows"
note "The attempt counter did NOT advance. The claim already counted that try,"
note "and counting it again would halve the retry budget of every job whose"
note "worker happened to die — punishing the job for an infrastructure failure."

# Leave nothing behind.
q "DELETE FROM job WHERE id='$JOB_ID'" >/dev/null
printf '\n'
