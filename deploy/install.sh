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

echo "==> Génération des services systemd (user=$SERVICE_USER, dir=$PROJECT_DIR)"
# night-light-experiment@ est un gabarit : il n'est ni activé ni démarré ici,
# une session se lance à la main (voir README, « Battery experiments »).
for unit in night-light.service night-light-experiment@.service; do
  sed -e "s|__USER__|$SERVICE_USER|g" \
      -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
      "deploy/$unit" | sudo tee "/etc/systemd/system/$unit" >/dev/null
done

sudo systemctl daemon-reload
sudo systemctl enable night-light
sudo systemctl restart night-light

echo "==> État du service"
sleep 2
sudo systemctl --no-pager status night-light | head -n 15
