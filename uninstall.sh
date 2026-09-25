#!/bin/bash
# Desinstalador — Removedor de Metadados & Alterador de Hash (macOS)
#   curl -fsSL https://raw.githubusercontent.com/PTK25/remove-hash/main/uninstall.sh | bash
# Remove o app e o comando de terminal. Não remove FFmpeg/Python/Homebrew.
set -euo pipefail

APP_NAME="Removedor de Metadados"
CLI_NAME="remove-hash"

removed=0
for dir in "/Applications" "$HOME/Applications"; do
  app="$dir/$APP_NAME.app"
  if [ -d "$app" ]; then
    pkill -f "$app/Contents" >/dev/null 2>&1 || true
    rm -rf "$app"
    echo "  ✓  Removido: $app"
    removed=1
  fi
done

for prefix in /opt/homebrew /usr/local; do
  link="$prefix/bin/$CLI_NAME"
  if [ -L "$link" ]; then
    rm -f "$link"
    echo "  ✓  Removido: $link"
    removed=1
  fi
done

if [ "$removed" = 1 ]; then
  echo "  ✓  Desinstalação concluída. (FFmpeg e Python foram mantidos; remova com: brew uninstall ffmpeg python-tk)"
else
  echo "  ›  Nada para remover — o app não estava instalado."
fi
