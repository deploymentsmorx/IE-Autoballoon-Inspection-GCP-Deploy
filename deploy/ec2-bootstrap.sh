#!/bin/bash
# First-time EC2 bootstrap for AutoBalloon (Ubuntu 22.04)
set -euo pipefail

echo "Installing Docker..."
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-plugin git curl
sudo systemctl enable docker
sudo systemctl start docker
sudo usermod -aG docker "$USER" || true

echo ""
echo "Next steps (as deploy user after re-login):"
echo "  git clone https://github.com/deploymentsmorx/Auto-Balloon-IE.git"
echo "  cd Auto-Balloon-IE"
echo "  cp deploy/.env.production.example .env.production"
echo "  nano .env.production"
echo "  docker compose -f docker-compose.prod.yml --env-file .env.production up -d --build"
