#!/usr/bin/env bash
set -uo pipefail

mkdir -p /logs/verifier
# Default to no reward; only a clean pytest pass overwrites this.
echo 0 > /logs/verifier/reward.txt

if [ "$PWD" = "/" ]; then
    echo "Error: No working directory set. Please set a WORKDIR in your Dockerfile before running this script."
    exit 1
fi

# The tests tree / fixtures location is parameterizable; default to /tests.
export TEST_DIR="${TEST_DIR:-/tests}"
export CANDIDATE_USER="${CANDIDATE_USER:-cert-candidate}"

# --- OS-isolate the verifier tree from candidate-controlled code ---
# The CLI and the repaired workflow are candidate-controlled and are executed as the unprivileged
# CANDIDATE_USER (see test_outputs.py). Locking $TEST_DIR root-only stops that code from reading
# the verifier's reference implementation or fixtures to fabricate passing artifacts; pytest
# itself stays root and can still read the locked tree. Control commands are called by absolute
# path so a shadow binary planted earlier in $PATH by the (root) agent cannot subvert the drop.
if [ "$(/usr/bin/id -u)" = "0" ] && /usr/bin/id "$CANDIDATE_USER" >/dev/null 2>&1; then
    /usr/bin/chown -R root:root "$TEST_DIR" 2>/dev/null || true
    /usr/bin/chmod 700 "$TEST_DIR" 2>/dev/null || true
    /usr/bin/find "$TEST_DIR" -mindepth 1 -exec /usr/bin/chmod go-rwx {} + 2>/dev/null || true

    # Establish the authoritative /app/output from an UNPRIVILEGED repair run: the graded
    # artifacts then provably come from candidate code that could not read $TEST_DIR, rather than
    # from whatever the agent left behind. The candidate needs to own the output directory and the
    # active workflow file it patches.
    /usr/bin/chown -R "$CANDIDATE_USER":"$CANDIDATE_USER" /app/output /app/workflow/export_report.py 2>/dev/null || true
    /usr/sbin/runuser -u "$CANDIDATE_USER" -- \
        /usr/local/bin/python3 /app/cert_audit.py repair --output-dir /app/output \
        >/logs/verifier/candidate_repair.log 2>&1 || true
fi

set +e
python3 -m pytest -o cache_dir=/tmp/pytest_cache -p no:cacheprovider \
  --ctrf /logs/verifier/ctrf.json "$TEST_DIR/test_outputs.py" -rA
RC=$?

if [ "$RC" -eq 0 ]; then
    echo 1 > /logs/verifier/reward.txt
else
    echo 0 > /logs/verifier/reward.txt
fi
