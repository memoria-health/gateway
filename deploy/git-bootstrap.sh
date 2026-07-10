#!/usr/bin/env bash
# Convert /opt/memoria-gateway into a GIT CHECKOUT of the fleet repo, so code
# comes from ONE source of truth (GitHub) and can't silently drift. Per-box
# identity files (cameras.json, .provisioned, .tunnel-port, wifi-connect/) are
# gitignored, so `git pull` updates CODE without ever touching a box's IDENTITY.
# That's the whole point: identical code everywhere, unique identity per box.
#
# Auth is a READ-ONLY GitHub deploy key baked into the template at
# ~/.ssh/memoria_deploy. Run this during the TEMPLATE build (prep-golden-image
# calls it), or as a deliberate one-time conversion — AS the owner of the dir.
#
# After this, deploying new code to a box is just:  git pull && sudo systemctl
# restart memoria-gateway  — and `git rev-parse HEAD` tells you exactly what it runs.
set -euo pipefail
DIR=/opt/memoria-gateway
REPO="git@github.com:potaton00b/nemara-gateway.git"
KEY="$HOME/.ssh/memoria_deploy"
[ -f "$KEY" ] || { echo "missing deploy key $KEY (bake it into the template first)"; exit 1; }
export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
cd "$DIR"
if [ ! -d .git ]; then
  git init -q
  git remote add origin "$REPO"
fi
git fetch -q origin main
# Reset TRACKED code to origin/main; gitignored identity files are left alone.
git checkout -q -f -B main origin/main
echo "/opt/memoria-gateway is now a git checkout @ $(git rev-parse --short HEAD)"
echo "deploy new code with:  cd $DIR && git pull && sudo systemctl restart memoria-gateway"
