#!/usr/bin/env bash
# Installe ou met à jour le service night-light.
# À lancer depuis le dossier du projet, sur le serveur : ./deploy/install.sh
# L'utilisateur et les chemins sont déduits de l'environnement courant.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="$(id -un)"
cd "$PROJECT_DIR"

if [[ ! -f .env ]]; then
  echo "Fichier .env manquant. Copie .env.example vers .env et complète-le." >&2
  exit 1
fi
chmod 600 .env

echo "==> Installation des dépendances"
poetry config virtualenvs.in-project true --local
poetry install --only main

if [[ ! -x "$PROJECT_DIR/.venv/bin/entrypoint" ]]; then
  echo "L'exécutable .venv/bin/entrypoint est introuvable après l'installation." >&2
  exit 1
fi

echo "==> Génération du service systemd (user=$SERVICE_USER, dir=$PROJECT_DIR)"
sed -e "s|__USER__|$SERVICE_USER|g" \
    -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    deploy/night-light.service | sudo tee /etc/systemd/system/night-light.service >/dev/null

sudo systemctl daemon-reload
sudo systemctl enable night-light
sudo systemctl restart night-light

echo "==> État du service"
sleep 2
sudo systemctl --no-pager status night-light | head -n 15
