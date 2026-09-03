#!/usr/bin/env bash
# One-shot setup for the k! relay bot on a fresh Oracle Cloud VM.
# Safe to re-run: it updates the code and restarts the service.
#
#   curl -fsSL https://raw.githubusercontent.com/muzziehater82-creator/relaybotk/main/deploy/setup-oracle.sh | bash
#
set -euo pipefail

REPO="https://github.com/muzziehater82-creator/relaybotk.git"
DIR="$HOME/relaybotk"
SERVICE="k-relay-bot"
RUN_USER="$(id -un)"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\n\033[1;33m!!  %s\033[0m\n' "$*"; }

if [ "$RUN_USER" = "root" ]; then
  warn "Run this as your normal user (ubuntu/opc), not root. Aborting."
  exit 1
fi

say "Installing system packages"
if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv python3-pip git
elif command -v dnf >/dev/null 2>&1; then
  sudo dnf install -y -q python3 python3-pip git
else
  warn "No apt-get or dnf found. Install python3, python3-venv and git manually."
  exit 1
fi

say "Fetching the bot"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only
else
  git clone --depth 1 "$REPO" "$DIR"
fi

say "Creating the virtualenv"
if [ ! -x "$DIR/.venv/bin/python" ]; then
  python3 -m venv "$DIR/.venv"
fi
"$DIR/.venv/bin/python" -m pip install --upgrade pip -q
"$DIR/.venv/bin/python" -m pip install -q -r "$DIR/requirements.txt"
"$DIR/.venv/bin/python" -c 'import discord; print("discord.py", discord.__version__)'

say "Running the offline test suite"
( cd "$DIR" && "$DIR/.venv/bin/python" test_logic.py >/dev/null && echo "all tests passed" )

# The token lives only here, never in git.
if [ ! -f "$DIR/.env" ]; then
  printf 'DISCORD_TOKEN=\n' > "$DIR/.env"
  chmod 600 "$DIR/.env"
  warn "No token yet. Add it now:"
  echo "      nano $DIR/.env"
  echo "  Put your token after DISCORD_TOKEN= then save (Ctrl+O, Enter, Ctrl+X)."
  echo "  Then re-run this script and it will finish the install."
  exit 0
fi
chmod 600 "$DIR/.env"

if ! grep -q '^DISCORD_TOKEN=.\+' "$DIR/.env"; then
  warn "DISCORD_TOKEN in $DIR/.env is still empty. Fill it in, then re-run this script."
  exit 1
fi

say "Installing the systemd service"
sed -e "s|__USER__|$RUN_USER|g" -e "s|__DIR__|$DIR|g" \
  "$DIR/deploy/$SERVICE.service" | sudo tee "/etc/systemd/system/$SERVICE.service" >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE" >/dev/null 2>&1
sudo systemctl restart "$SERVICE"

sleep 4
say "Service status"
sudo systemctl --no-pager --lines=0 status "$SERVICE" || true
say "Recent log output"
sudo journalctl -u "$SERVICE" -n 20 --no-pager || true

cat <<EOF

------------------------------------------------------------
Done. The bot now starts on boot and restarts if it crashes.

  live logs     : sudo journalctl -u $SERVICE -f
  restart       : sudo systemctl restart $SERVICE
  stop          : sudo systemctl stop $SERVICE
  update to HEAD: bash $DIR/deploy/setup-oracle.sh
------------------------------------------------------------
EOF
