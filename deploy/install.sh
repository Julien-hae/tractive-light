#!/usr/bin/env bash
# Installe ou met à jour le service night-light sur le VPS.
# À lancer depuis le dossier du projet, sur le serveur : ./deploy/install.sh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

if [[ ! -f .env ]]; then
  echo "Fichier .env manquant. Copie .env.example vers .env et complète-le." >&2
  exit 1
fi
chmod 600 .env

echo "==> Installation des dépendances"
poetry config virtualenvs.in-project true --local
poetry install --only main

echo "==> Installation du service systemd"
sudo cp deploy/night-light.service /etc/systemd/system/night-light.service
sudo systemctl daemon-reload
sudo systemctl enable --now night-light

echo "==> État du service"
sudo systemctl --no-pager status night-light | head -n 12
