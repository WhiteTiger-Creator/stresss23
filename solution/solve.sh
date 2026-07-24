#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# --- Host containment per /app/docs/containment_runbook.md ---
# Revoke the automation's SSH persistence surgically so the ops on-call key survives.
if [ -f /root/.ssh/authorized_keys ]; then
  grep -v 'casync-automation' /root/.ssh/authorized_keys > /root/.ssh/authorized_keys.tmp || true
  mv /root/.ssh/authorized_keys.tmp /root/.ssh/authorized_keys
  chmod 600 /root/.ssh/authorized_keys
fi

# Remove the passwordless sudoers escalation entirely.
rm -f /etc/sudoers.d/casync

# Lock down the exposed stream signing key: keep it, restrict to root 0600.
chown root:root /app/secrets/cert_signing.key
chmod 0600 /app/secrets/cert_signing.key

cp "${SCRIPT_DIR}/cert_audit.py" /app/cert_audit.py
chmod +x /app/cert_audit.py
mkdir -p /app/output /app/audit

# Ensure frozen broken snapshot exists for pre-repair audit reads.
if [ ! -f /app/workflow/.export_report.original ]; then
  cp /app/workflow/export_report.py /app/workflow/.export_report.original
  chmod a-w /app/workflow/.export_report.original
fi

python3 /app/cert_audit.py diagnose \
  --dossier /app/incident/export_dossier.md \
  --report /app/output/diagnosis.json

python3 /app/cert_audit.py repair --output-dir /app/output

# Compatibility copy for harnesses that read /app/audit.
cp /app/output/diagnosis.json /app/audit/diagnosis.json
cp /app/output/repair_audit.json /app/audit/repair_audit.json
