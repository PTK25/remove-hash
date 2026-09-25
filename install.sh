#!/bin/bash
# ═══════════════════════════════════════════════════════════════════
#  Instalador — Removedor de Metadados & Alterador de Hash (macOS)
#
#  Instalação em uma linha (cole no Terminal):
#    curl -fsSL https://raw.githubusercontent.com/PTK25/remove-hash/main/install.sh | bash
#
#  Ou, com o repositório já baixado/clonado:
#    ./install.sh
#
#  O que ele faz:
#    1. Instala o Homebrew, se faltar (pede confirmação).
#    2. Instala o FFmpeg e o Python com Tkinter via Homebrew.
#    3. Monta o app "Removedor de Metadados.app" em /Applications.
#    4. Cria o comando de terminal `remove-hash`.
#  Como o app é montado localmente, o macOS não bloqueia a abertura
#  (não há aviso de "desenvolvedor não identificado").
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail

APP_NAME="Removedor de Metadados"
BUNDLE_ID="com.ptk25.removedor-metadados"
REPO="PTK25/remove-hash"
BRANCH="main"
CLI_NAME="remove-hash"
VERSION="2.0.0"

if [ -t 1 ]; then
  B="\033[1m"; D="\033[2m"; G="\033[32m"; Y="\033[33m"; R="\033[31m"; C="\033[36m"; N="\033[0m"
else
  B=""; D=""; G=""; Y=""; R=""; C=""; N=""
fi
ok()   { printf "  ${G}✓${N}  %s\n" "$*"; }
info() { printf "  ${C}›${N}  %s\n" "$*"; }
warn() { printf "  ${Y}!${N}  %s\n" "$*"; }
fail() { printf "  ${R}✗${N}  %s\n" "$*" >&2; exit 1; }
ask()  { # ask "pergunta" → retorna 0 para sim
  local answer
  if [ -r /dev/tty ]; then
    printf "  ${Y}?${N}  %s [S/n] " "$1"
    read -r answer < /dev/tty || answer=""
  else
    answer="s"
  fi
  case "${answer:-s}" in [sSyY]*|"") return 0 ;; *) return 1 ;; esac
}

printf "\n${B}🛡️  Instalador — ${APP_NAME} v${VERSION}${N}\n"
printf "${D}────────────────────────────────────────────────────────${N}\n\n"

[ "$(uname -s)" = "Darwin" ] || fail "Este instalador é só para macOS. Em Linux: instale ffmpeg e python3-tk e rode: python3 remove_hash_gui.py"

# ── 1. Fontes: pasta local ou download do GitHub ─────────────────
SRC_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
if [ -z "$SRC_DIR" ] || [ ! -f "$SRC_DIR/remove_hash_core.py" ]; then
  info "Baixando a versão mais recente de github.com/${REPO}…"
  TMP_DIR="$(mktemp -d)"
  trap 'rm -rf "$TMP_DIR"' EXIT
  curl -fsSL "https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz" | tar -xz -C "$TMP_DIR" \
    || fail "Não foi possível baixar o projeto. Verifique sua conexão."
  SRC_DIR="$(find "$TMP_DIR" -maxdepth 1 -type d -name 'remove-hash-*' | head -1)"
  [ -f "$SRC_DIR/remove_hash_core.py" ] || fail "Download incompleto."
fi
ok "Fontes: $SRC_DIR"

# ── 2. Homebrew ───────────────────────────────────────────────────
load_brew() {
  if ! command -v brew >/dev/null 2>&1; then
    for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
      if [ -x "$b" ]; then eval "$("$b" shellenv)"; break; fi
    done
  fi
  command -v brew >/dev/null 2>&1
}
if ! load_brew; then
  warn "Homebrew não encontrado. Ele é necessário para instalar o FFmpeg e o Python com Tkinter."
  if ask "Instalar o Homebrew agora? (vai pedir sua senha)"; then
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" < /dev/tty \
      || fail "A instalação do Homebrew falhou."
    load_brew || fail "Homebrew instalado, mas não encontrado no PATH. Feche e reabra o Terminal e rode o instalador de novo."
  else
    fail "Instalação cancelada. Instale o Homebrew em https://brew.sh e rode de novo."
  fi
fi
BREW_PREFIX="$(brew --prefix)"
ok "Homebrew em $BREW_PREFIX"

# ── 3. FFmpeg ─────────────────────────────────────────────────────
if command -v ffmpeg >/dev/null 2>&1 || [ -x "$BREW_PREFIX/bin/ffmpeg" ]; then
  ok "FFmpeg já instalado ($("$BREW_PREFIX/bin/ffmpeg" -version 2>/dev/null | head -1 | awk '{print $3}' || ffmpeg -version | head -1 | awk '{print $3}'))"
else
  info "Instalando FFmpeg (pode levar alguns minutos)…"
  brew install ffmpeg || fail "Falha ao instalar o FFmpeg (brew install ffmpeg)."
  ok "FFmpeg instalado"
fi

# ── 4. Python com Tkinter ─────────────────────────────────────────
find_python() {
  local c
  for c in "$BREW_PREFIX/bin/python3" /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3 2>/dev/null || true)"; do
    [ -n "$c" ] && [ -x "$c" ] || continue
    if "$c" -c 'import sys, tkinter; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
      echo "$c"; return 0
    fi
  done
  return 1
}
PY="$(find_python || true)"
if [ -z "$PY" ]; then
  info "Instalando Python com Tkinter (brew install python-tk)…"
  brew install python-tk || fail "Falha ao instalar python-tk."
  PY="$(find_python || true)"
  [ -n "$PY" ] || fail "Python com Tkinter não encontrado mesmo após a instalação."
fi
ok "Python: $PY ($("$PY" -c 'import sys, tkinter; print(f"{sys.version.split()[0]}, Tk {tkinter.TkVersion}")'))"

# ── 5. Montar o .app ──────────────────────────────────────────────
DEST="/Applications"
[ -w "$DEST" ] || { DEST="$HOME/Applications"; mkdir -p "$DEST"; }
APP="$DEST/$APP_NAME.app"
if [ -d "$APP" ]; then
  pkill -f "$APP/Contents" >/dev/null 2>&1 || true
  rm -rf "$APP"
fi
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$SRC_DIR/remove_hash_core.py" "$SRC_DIR/remove_hash_gui.py" "$SRC_DIR/remove_hash.py" "$APP/Contents/Resources/"
cp "$SRC_DIR/assets/icon.icns" "$APP/Contents/Resources/icon.icns"
cp "$SRC_DIR/assets/icon.png" "$APP/Contents/Resources/icon.png"
chmod +x "$APP/Contents/Resources/remove_hash.py" "$APP/Contents/Resources/remove_hash_gui.py"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>$APP_NAME</string>
  <key>CFBundleDisplayName</key><string>$APP_NAME</string>
  <key>CFBundleIdentifier</key><string>$BUNDLE_ID</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleExecutable</key><string>launcher</string>
  <key>CFBundleIconFile</key><string>icon.icns</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleDevelopmentRegion</key><string>pt_BR</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.utilities</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSHumanReadableCopyright</key><string>Código aberto — github.com/$REPO</string>
</dict>
</plist>
PLIST

# Launcher: acha um Python com Tkinter e abre a interface.
# Usa uma cópia do binário do Python dentro do app para que o Dock e a barra
# de menus mostrem o nome e o ícone do app (e não "Python"). Se o Python do
# Homebrew for atualizado, a cópia é refeita automaticamente.
cat > "$APP/Contents/MacOS/launcher" <<'LAUNCHER'
#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
RES="$DIR/../Resources"
RUNTIME="$DIR/python-runtime"
export PATH="/opt/homebrew/bin:/usr/local/bin:/opt/local/bin:$PATH"

works() { [ -x "$1" ] && "$1" -c 'import tkinter' >/dev/null 2>&1; }

find_python() {
  local c
  for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3 2>/dev/null || true)" /usr/bin/python3; do
    [ -n "$c" ] && works "$c" && { echo "$c"; return 0; }
  done
  return 1
}

refresh_runtime() {
  local prefix bin
  prefix="$("$1" -c 'import sys; print(sys.prefix)' 2>/dev/null)" || return 1
  bin="$prefix/Resources/Python.app/Contents/MacOS/Python"
  [ -x "$bin" ] || return 1
  cp -f "$bin" "$RUNTIME" 2>/dev/null && chmod +x "$RUNTIME" && works "$RUNTIME"
}

if ! works "$RUNTIME"; then
  PY="$(find_python)" || {
    osascript -e 'display dialog "Python com Tkinter não foi encontrado.\n\nAbra o Terminal e rode o instalador novamente:\n\ncurl -fsSL https://raw.githubusercontent.com/PTK25/remove-hash/main/install.sh | bash" buttons {"OK"} default button 1 with icon stop with title "Removedor de Metadados"' >/dev/null 2>&1
    exit 1
  }
  refresh_runtime "$PY" || exec "$PY" "$RES/remove_hash_gui.py" "$@"
fi
exec "$RUNTIME" "$RES/remove_hash_gui.py" "$@"
LAUNCHER
chmod +x "$APP/Contents/MacOS/launcher"

# Cópia inicial do runtime (nome/ícone corretos no Dock)
PY_PREFIX="$("$PY" -c 'import sys; print(sys.prefix)')"
PY_APP_BIN="$PY_PREFIX/Resources/Python.app/Contents/MacOS/Python"
if [ -x "$PY_APP_BIN" ]; then
  cp -f "$PY_APP_BIN" "$APP/Contents/MacOS/python-runtime" && chmod +x "$APP/Contents/MacOS/python-runtime"
  if ! "$APP/Contents/MacOS/python-runtime" -c 'import tkinter' >/dev/null 2>&1; then
    rm -f "$APP/Contents/MacOS/python-runtime"
  fi
fi

xattr -cr "$APP" 2>/dev/null || true
touch "$APP"
ok "App instalado em $APP"

# ── 6. Comando de terminal ────────────────────────────────────────
if [ -w "$BREW_PREFIX/bin" ]; then
  ln -sf "$APP/Contents/Resources/remove_hash.py" "$BREW_PREFIX/bin/$CLI_NAME"
  ok "Comando de terminal: $CLI_NAME  (ex.: $CLI_NAME ~/Fotos -o ~/Desktop)"
else
  warn "Sem permissão para criar o comando em $BREW_PREFIX/bin (pulado)."
fi

printf "\n${D}────────────────────────────────────────────────────────${N}\n"
printf "  ${G}${B}Pronto!${N}  Abra \"${APP_NAME}\" pelo Launchpad ou Spotlight.\n"
printf "  ${D}Para desinstalar: curl -fsSL https://raw.githubusercontent.com/$REPO/$BRANCH/uninstall.sh | bash${N}\n\n"

if [ -r /dev/tty ] && [ "${REMOVE_HASH_NO_OPEN:-}" != "1" ]; then
  open "$APP" || true
fi
