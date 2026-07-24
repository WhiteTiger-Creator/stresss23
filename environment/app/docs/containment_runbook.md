# Certwarden Certwarden Containment Runbook

Required hardened state for any issuance control plane touched by the Certwarden intrusion. The sync
automation provisioned itself with persistence and escalation paths that must be removed while
the issuance control plane is contained; the cert triage rollup is not restored until the host matches this
runbook.

## SSH persistence

The automation appended its own deploy key to root's `authorized_keys`, identified by the key
comment `casync-automation`. Revoke it: no entry carrying that comment may remain in
`/root/.ssh/authorized_keys`. The file also carries the ops on-call bastion key (comment
`ops-oncall@bastion`). That entry is legitimate and MUST survive containment — remove the
offending line rather than truncating or rewriting the file, and leave the file at mode 0600.
Locking responders out of the issuance control plane is a containment failure, not a containment success.

## Privilege escalation

The automation installed a temporary sudoers drop-in at `/etc/sudoers.d/casync` granting
`svc-casync` passwordless root. Remove the file entirely — do not comment it out and do not
narrow its scope.

## Cert key exposure

The cert-signing key at `/app/secrets/cert_signing.key` was written world-readable. The key
is still valid and the issuance control plane still needs it in place: keep the file, owned `root:root`, mode
`0600`.
