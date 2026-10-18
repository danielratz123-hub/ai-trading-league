#!/bin/sh
# Stage 1 host hardening (SECURITY.md). Run ON THE SERVER, from the project
# folder, in an SSH session you keep open until a NEW login has worked:
#
#   sudo ./scripts/harden-host.sh
#
# Safe by design:
#   - every file it changes is copied to <file>.bak-<date> first
#   - `sshd -t` checks the new SSH config; if it fails, the backups are put
#     back and nothing is reloaded
#   - SSH is RELOADED, not restarted: open sessions stay connected
#   - the firewall is only checked, never changed
#   - the league (Docker) is not touched
#
# What it does:
#   1. SSH: AllowUsers daniel, no X11/agent forwarding, and the main
#      sshd_config's leftover "PermitRootLogin yes" set to "no" (the drop-ins
#      already win, this just removes the trap). TCP forwarding stays ON: the
#      setup page is opened through `ssh -L`.
#   2. The cloud image's default `ubuntu` user (sudo + lxd, never used):
#      locked, expired, removed from sudo/lxd, its SSH keys set aside.
#   3. fail2ban: never ban Tailscale addresses (your way back in), and a
#      `recidive` jail that bans repeat offenders for a week.
#   4. Prints ufw / fail2ban / sshd state so it can go into SECURITY.md.
#
# Undo: copy the .bak-<date> files back and `systemctl reload ssh`; for the
# ubuntu user see the commands printed at the end.
set -eu

ADMIN=daniel
TS=$(date +%Y%m%d-%H%M%S)

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo."; exit 1; }
[ -s "/home/$ADMIN/.ssh/authorized_keys" ] || { echo "/home/$ADMIN/.ssh/authorized_keys is empty: refusing (you would be locked out)."; exit 1; }
grep -qE '^(ssh-|ecdsa-|sk-)' "/home/$ADMIN/.ssh/authorized_keys" \
  || grep -qE ' (ssh-|ecdsa-|sk-)' "/home/$ADMIN/.ssh/authorized_keys" \
  || { echo "No usable key in /home/$ADMIN/.ssh/authorized_keys: refusing."; exit 1; }

cat <<EOF
This will harden SSH, lock the unused 'ubuntu' user and extend fail2ban.
Before you continue:
  - keep THIS session open until a new login has worked
  - make sure the Contabo VNC console works (your fallback)
EOF
printf "Type 'harden' to continue: "
read -r answer
[ "$answer" = "harden" ] || { echo "Cancelled."; exit 1; }

backup() { [ -e "$1" ] && cp -a "$1" "$1.bak-$TS" && echo "  backup: $1.bak-$TS"; return 0; }

echo "== 1. SSH"
MAIN=/etc/ssh/sshd_config
DROP=/etc/ssh/sshd_config.d/01-league.conf
backup "$MAIN"
backup "$DROP"
sed -i -E 's/^[[:space:]]*PermitRootLogin[[:space:]]+yes/PermitRootLogin no/; s/^[[:space:]]*X11Forwarding[[:space:]]+yes/X11Forwarding no/' "$MAIN"
cat > "$DROP" <<EOF
# Added by scripts/harden-host.sh on $TS (see SECURITY.md).
# Loaded before the other drop-ins, so these values win.
AllowUsers $ADMIN
X11Forwarding no
AllowAgentForwarding no
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
EOF
chmod 644 "$DROP"
if ! sshd -t; then
  echo "!! New SSH config is invalid: restoring the backups, nothing reloaded."
  cp -a "$MAIN.bak-$TS" "$MAIN"
  if [ -e "$DROP.bak-$TS" ]; then cp -a "$DROP.bak-$TS" "$DROP"; else rm -f "$DROP"; fi
  exit 1
fi
systemctl reload ssh
echo "  sshd reloaded (open sessions are unaffected)"

echo "== 2. Unused 'ubuntu' user"
if id ubuntu >/dev/null 2>&1; then
  if pgrep -u ubuntu >/dev/null 2>&1; then
    echo "  !! ubuntu has running processes: skipped. Look at: ps -u ubuntu"
  else
    usermod -L ubuntu
    chage -E 0 ubuntu
    for g in sudo lxd; do gpasswd -d ubuntu "$g" >/dev/null 2>&1 && echo "  removed from $g" || true; done
    if [ -e /home/ubuntu/.ssh/authorized_keys ]; then
      mv /home/ubuntu/.ssh/authorized_keys "/home/ubuntu/.ssh/authorized_keys.disabled-$TS"
      echo "  SSH keys set aside: /home/ubuntu/.ssh/authorized_keys.disabled-$TS"
    fi
    echo "  locked and expired"
  fi
fi

echo "== 3. fail2ban"
F2B=/etc/fail2ban/jail.d/league.local
backup "$F2B"
cat > "$F2B" <<EOF
# Added by scripts/harden-host.sh on $TS (see SECURITY.md).
[DEFAULT]
# Never ban localhost or Tailscale (100.64.0.0/10): your way back in.
ignoreip = 127.0.0.1/8 ::1 100.64.0.0/10 fd7a:115c:a1e0::/48

# Addresses banned again and again: one week.
[recidive]
enabled  = true
backend  = auto
logpath  = /var/log/fail2ban.log
bantime  = 7d
findtime = 1d
maxretry = 3
EOF
if fail2ban-client -t >/dev/null 2>&1; then
  systemctl reload fail2ban && echo "  fail2ban reloaded"
else
  echo "  !! fail2ban config test failed: restoring"
  if [ -e "$F2B.bak-$TS" ]; then cp -a "$F2B.bak-$TS" "$F2B"; else rm -f "$F2B"; fi
fi

echo "== 4. State (for SECURITY.md)"
sshd -T | grep -Ei '^(permitrootlogin|passwordauthentication|kbdinteractiveauthentication|pubkeyauthentication|x11forwarding|allowagentforwarding|allowtcpforwarding|allowusers|maxauthtries) '
echo "--"; ufw status verbose
ufw status | grep -qE '^(22|OpenSSH)[ /]' || echo "  !! ufw has no rule for SSH (22). Do NOT log out; run: ufw allow OpenSSH"
echo "--"; fail2ban-client status | sed -n 2,3p
echo "--"; passwd -S ubuntu 2>/dev/null || true
cat <<EOF

Done. NOW, from a NEW terminal on your PC, check you can still log in:
    ssh $ADMIN@<server IP>
Only close this session once that works.

Undo the ubuntu lock if ever needed:
    usermod -U ubuntu; chage -E -1 ubuntu; gpasswd -a ubuntu sudo
EOF
