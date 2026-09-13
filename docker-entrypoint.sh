#!/bin/sh
# Cloud-GPU providers (Vast.ai --ssh --direct, etc.) expect the box to be
# SSH-reachable once it's "running". This image's own ENTRYPOINT used to be
# the awnode gateway process directly (PID 1, foreground) with no sshd
# at all — on Vast.ai that meant the box reached RUNNING but nothing was
# ever listening on the mapped SSH port ("Connection refused", not a
# timeout/auth failure), so onboarding could never pass its SSH readiness
# check. Start sshd in the background first, then exec the real entrypoint
# in the foreground so container health/liveness still tracks the real app.
# Real Vast.ai test proved this provider's own account-key injection can't be
# trusted for a bespoke (non-template) image: the box reached RUNNING with
# sshd reachable (full SSH handshake, the provider's own connection banner),
# but every connection got "Permission denied (publickey)" despite the key
# being genuinely registered on the account. Authorization is now handled
# explicitly by the onstart script instead (see
# AitherOS/services/mesh/providers/vast_ai.py's build_node_agent_onstart,
# ssh_authorized_key param) — deliberately NOT touching /root/.ssh here so
# there's no ordering/ownership conflict with whatever that script (or the
# provider itself) writes there afterward.
set -e

mkdir -p /run/sshd
ssh-keygen -A >/dev/null 2>&1 || true
/usr/sbin/sshd

exec "$@"
