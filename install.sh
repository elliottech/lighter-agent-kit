#!/usr/bin/env bash
# 
# Lighter Agent Kit — Installation Script
#
# Usage:
#  curl -fsSL https://github.com/elliottech/lighter-agent-kit/releases/latest/download/install.sh | bash
#
# Or download and run directly:
#  bash install.sh
# 

set -euo pipefail

# Bash guard 
# The script uses bash-specific features (arrays, [[ ]], (( ))). If someone
# pipes with `| sh` on a system where sh is dash/ash, this catches it early.
if [ -z "${BASH_VERSION:-}" ]; then
  printf 'Error: this installer requires bash.\n' >&2
  printf 'Run:  curl -fsSL <url> | bash\n' >&2
  exit 1
fi

# Constants 

REPO_URL="https://github.com/elliottech/lighter-agent-kit.git"
SKILL_NAME="lighter-agent-kit"
GUM_VERSION="0.14.5"
MIN_PYTHON_MAJOR=3
MIN_PYTHON_MINOR=9
CREDENTIALS_DIR="${HOME}/.lighter/lighter-agent-kit"

# Mutable state 

OS=""
ARCH=""
PYTHON_CMD=""
INSTALL_DIR=""
EXTRA_DIRS=()
GUM_CMD=""
USE_GUM=false
TEMP_DIR=""

# Cleanup on exit / interrupt 

cleanup() {
  if [[ -n "${TEMP_DIR:-}" && -d "${TEMP_DIR:-}" ]]; then
    rm -rf "$TEMP_DIR"
  fi
}
trap cleanup EXIT INT TERM

# Colors 

if [[ -t 1 ]] && [[ "${TERM:-dumb}" != "dumb" ]]; then
  RED='\033[0;31m'
  GREEN='\033[0;32m'
  YELLOW='\033[0;33m'
  BLUE='\033[0;34m'
  BOLD='\033[1m'
  DIM='\033[2m'
  RESET='\033[0m'
else
  RED='' GREEN='' YELLOW='' BLUE='' BOLD='' DIM='' RESET=''
fi

info()    { printf "${BLUE}•${RESET} %s\n" "$*"; }
success() { printf "${GREEN}✓${RESET} %s\n" "$*"; }
warn()    { printf "${YELLOW}!${RESET} %s\n" "$*" >&2; }
error()   { printf "${RED}✗${RESET} %s\n" "$*" >&2; }

# Helpers 

has_cmd() { command -v "$1" &>/dev/null; }

read_tty() {
  if [[ -t 0 ]]; then
    read "$@"
    return $?
  fi
  if [[ -e /dev/tty ]]; then
    if { read "$@" </dev/tty; } 2>/dev/null; then
      return 0
    fi
  fi
  error "No terminal available for interactive input."
  error "Download the script and run it directly:  bash install.sh"
  exit 1
}

# Run gum with stdin wired to the terminal.
run_gum() {
  if [[ -t 0 ]]; then
    "$GUM_CMD" "$@"
  elif [[ -e /dev/tty ]]; then
    "$GUM_CMD" "$@" </dev/tty 2>/dev/tty || return 1
  else
    return 1
  fi
}

download() {
  local url="$1" dest="$2"
  if has_cmd curl; then
    curl -fsSL --connect-timeout 10 "$url" -o "$dest"
  elif has_cmd wget; then
    wget -qO "$dest" --timeout=10 "$url"
  else
    return 1
  fi
}

run_privileged_command() {
  local cmd="$1"

  if (( EUID == 0 )); then
    bash -lc "$cmd"
    return 0
  fi

  if has_cmd sudo; then
    sudo bash -lc "$cmd"
    return 0
  fi

  error "This step requires elevated privileges, but sudo is not installed."
  info  "Re-run this installer as root, or install sudo and try again."
  exit 1
}

show_privileged_command() {
  local cmd="$1"

  if (( EUID == 0 )); then
    printf '%s' "$cmd"
  else
    printf 'sudo %s' "$cmd"
  fi
}

# TUI wrappers (gum with plain-text fallback) 

header() {
  local text="$1"
  if $USE_GUM; then
    echo ""
    run_gum style \
      --border rounded \
      --border-foreground 39 \
      --padding "0 2" \
      --bold \
      "$text" || printf "\n${BOLD}  %s${RESET}\n\n" "$text"
  else
    echo ""
    printf "${BOLD}  %s${RESET}\n" "$text"
    echo ""
  fi
}

# choose "prompt" "option1" "option2" ...  →  prints selected option to stdout
choose() {
  local prompt="$1"; shift
  local options=("$@")

  # choose() is called inside $() so stdout is captured as the return value.
  # User-visible text (prompt, menu) must go to stderr; only the result to stdout.

  if $USE_GUM; then
    printf "${BOLD}%s${RESET}\n" "$prompt" >&2
    local result
    result=$(run_gum choose --cursor "› " "${options[@]}") || true
    if [[ -n "${result:-}" ]]; then
      echo "$result"
      return 0
    fi
    # Fallback if gum exited without selection (Ctrl+C, broken pipe, etc.)
  fi

  # Plain numbered menu (primary when gum unavailable, fallback when gum fails)
  printf "${BOLD}%s${RESET}\n" "$prompt" >&2
  local i=1
  for opt in "${options[@]}"; do
    printf "  ${DIM}[%d]${RESET} %s\n" "$i" "$opt" >&2
    ((i++))
  done
  while true; do
    printf "  Choice [1-%d]: " "${#options[@]}" >&2
    local choice=""
    read_tty -r choice || exit 1
    if [[ "$choice" =~ ^[0-9]+$ ]] && (( choice >= 1 && choice <= ${#options[@]} )); then
      echo "${options[$((choice - 1))]}"
      return 0
    fi
    warn "Please enter a number between 1 and ${#options[@]}."
  done
}

# confirm "prompt" [default: yes|no]  →  returns 0 (yes) or 1 (no)
confirm() {
  local prompt="$1"
  local default="${2:-yes}"

  if $USE_GUM; then
    local flag="--default=yes"
    [[ "$default" == "no" ]] && flag="--default=no"
    if run_gum confirm "$flag" "$prompt"; then
      return 0
    else
      return 1
    fi
  fi

  local hint="[Y/n]"
  [[ "$default" == "no" ]] && hint="[y/N]"
  printf "${BOLD}%s${RESET} %s " "$prompt" "$hint"
  local answer
  read_tty -r answer
  answer="${answer:-$default}"
  case "${answer,,}" in
    y|yes) return 0 ;;
    *)     return 1 ;;
  esac
}

input_text() {
  local prompt="$1"
  local placeholder="${2:-}"
  local default="${3:-}"

  if $USE_GUM; then
    local result
    result=$(run_gum input \
      --prompt "$prompt: " \
      --placeholder "$placeholder" \
      --value "$default") || true
    if [[ -n "${result:-}" ]]; then
      echo "$result"
      return 0
    fi
  fi

  # Fallback
  if [[ -n "$default" ]]; then
    printf "%s [%s]: " "$prompt" "$default" >&2
  elif [[ -n "$placeholder" ]]; then
    printf "%s (%s): " "$prompt" "$placeholder" >&2
  else
    printf "%s: " "$prompt" >&2
  fi
  local value
  read_tty -r value
  echo "${value:-$default}"
}

input_secret() {
  local prompt="$1"

  if $USE_GUM; then
    local result
    result=$(run_gum input --password --prompt "$prompt: ") || result=""
    if [[ -n "${result:-}" ]]; then
      echo "$result"
      return 0
    fi
    # Fall through to native path if gum exited without a value
  fi

  printf "%s ${DIM}(input hidden)${RESET}: " "$prompt" >&2
  local value
  read_tty -rs value
  echo "" >&2
  echo "$value"
}

spin() {
  local title="$1"; shift

  if $USE_GUM; then
    run_gum spin --spinner dot --title "$title" -- "$@" && return 0
  fi
  info "$title"
  "$@"
}

# Platform detection 

detect_platform() {
  local os_name arch_name
  os_name="$(uname -s)"
  arch_name="$(uname -m)"

  case "$os_name" in
    Darwin) OS="darwin" ;;
    Linux)  OS="linux" ;;
    MINGW*|MSYS*|CYGWIN*)
      error "Windows is not supported directly."
      info  "Use WSL (Windows Subsystem for Linux) instead:"
      info  "  https://learn.microsoft.com/en-us/windows/wsl/install"
      exit 1 ;;
    *)
      error "Unsupported operating system: $os_name"
      exit 1 ;;
  esac

  case "$arch_name" in
    x86_64|amd64)  ARCH="amd64" ;;
    aarch64|arm64) ARCH="arm64" ;;
    *)
      error "Unsupported architecture: $arch_name"
      exit 1 ;;
  esac

  if [[ "$OS" == "darwin" && "$ARCH" == "amd64" ]]; then
    error "macOS Intel (x86_64) is not supported."
    info  "The native signer binary requires Apple Silicon (M1/M2/M3/M4)."
    exit 1
  fi
}

# gum (TUI toolkit) 
# Downloaded to a temp dir — zero system footprint, cleaned up on exit.

install_gum() {
  if has_cmd gum; then
    GUM_CMD="gum"
    USE_GUM=true
    return 0
  fi

  # Map our platform names to gum's release naming convention
  local gum_os gum_arch
  case "$OS" in
    darwin) gum_os="Darwin" ;;
    linux)  gum_os="Linux" ;;
    *)      return 0 ;;  # skip silently
  esac
  case "$ARCH" in
    amd64) gum_arch="x86_64" ;;
    arm64) gum_arch="arm64" ;;
    *)     return 0 ;;
  esac

  TEMP_DIR="$(mktemp -d 2>/dev/null || mktemp -d -t lighter_install)"
  local archive="gum_${GUM_VERSION}_${gum_os}_${gum_arch}.tar.gz"
  local url="https://github.com/charmbracelet/gum/releases/download/v${GUM_VERSION}/${archive}"
  local dest="${TEMP_DIR}/${archive}"

  if download "$url" "$dest" 2>/dev/null; then
    tar -xzf "$dest" -C "$TEMP_DIR" 2>/dev/null || return 0
    # gum tarballs extract to a subdirectory
    local found
    found="$(find "$TEMP_DIR" -name gum -type f 2>/dev/null | head -1)"
    if [[ -n "$found" && -f "$found" ]]; then
      chmod +x "$found"
      GUM_CMD="$found"
      USE_GUM=true
    fi
  fi
  # Failure is silent — plain menus are always available as fallback.
}

# Python detection 

find_python() {
  local best_cmd="" best_major=0 best_minor=0

  for cmd in python3 python python3.14 python3.13 python3.12 python3.11 python3.10 python3.9; do
    if ! has_cmd "$cmd"; then
      continue
    fi
    local raw_version major minor
    raw_version=$("$cmd" --version 2>&1) || continue
    # Extract "3.12" from "Python 3.12.4"
    raw_version="${raw_version#Python }"
    major="${raw_version%%.*}"
    minor="${raw_version#*.}"
    minor="${minor%%.*}"

    # Validate they're numbers
    [[ "$major" =~ ^[0-9]+$ ]] || continue
    [[ "$minor" =~ ^[0-9]+$ ]] || continue

    if (( major < MIN_PYTHON_MAJOR )); then continue; fi
    if (( major == MIN_PYTHON_MAJOR && minor < MIN_PYTHON_MINOR )); then continue; fi

    if (( major > best_major || (major == best_major && minor > best_minor) )); then
      best_cmd="$cmd"
      best_major="$major"
      best_minor="$minor"
    fi
  done

  if [[ -n "$best_cmd" ]]; then
    PYTHON_CMD="$best_cmd"
    return 0
  fi
  return 1
}

offer_python_install() {
  warn "Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+ is required but was not found."
  echo ""

  if ! confirm "Would you like to install Python?"; then
    error "Cannot continue without Python."
    info  "Install Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+ manually, then re-run this script."
    exit 1
  fi

  local install_cmd=""
  local needs_privileges=false

  if [[ "$OS" == "darwin" ]]; then
    if has_cmd brew; then
      install_cmd="brew install python@3.12"
    else
      error "Homebrew not found."
      info  "Install Homebrew first:  /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
      info  "Then re-run this installer."
      exit 1
    fi
  elif [[ "$OS" == "linux" ]]; then
    if has_cmd apt-get; then
      install_cmd="apt-get update && apt-get install -y python3 python3-pip python3-venv"
      needs_privileges=true
    elif has_cmd dnf; then
      install_cmd="dnf install -y python3 python3-pip"
      needs_privileges=true
    elif has_cmd yum; then
      install_cmd="yum install -y python3 python3-pip"
      needs_privileges=true
    elif has_cmd pacman; then
      install_cmd="pacman -S --noconfirm python python-pip"
      needs_privileges=true
    elif has_cmd apk; then
      install_cmd="apk add --no-cache python3 py3-pip"
      needs_privileges=true
    else
      error "No supported package manager found (tried: apt, dnf, yum, pacman, apk)."
      info  "Install Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+ manually, then re-run."
      exit 1
    fi
  fi

  if $needs_privileges; then
    local display_cmd
    display_cmd="$(show_privileged_command "$install_cmd")"
    info  "Will run:  ${display_cmd}"
    warn  "This requires elevated privileges."
    if ! confirm "Continue?" "yes"; then
      error "Cannot continue without Python."
      exit 1
    fi
    run_privileged_command "$install_cmd"
  else
    info "Will run:  ${install_cmd}"
    bash -lc "$install_cmd"
  fi

  if ! find_python; then
    error "Python installation did not produce a working Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+."
    info  "You may need to restart your shell, then re-run this script."
    exit 1
  fi

  success "Python installed: $("$PYTHON_CMD" --version 2>&1)"
}

# Git detection 

check_git() {
  if has_cmd git; then
    return 0
  fi

  warn "git is required but was not found."

  if ! confirm "Would you like to install git?"; then
    error "Cannot continue without git."
    exit 1
  fi

  local install_cmd="" needs_privileges=false

  if [[ "$OS" == "darwin" ]]; then
    info "On macOS, git comes with the Xcode Command Line Tools."
    info "Running: xcode-select --install"
    info "Follow the dialog, then re-run this script when it finishes."
    xcode-select --install 2>/dev/null || true
    exit 0
  elif [[ "$OS" == "linux" ]]; then
    needs_privileges=true
    if   has_cmd apt-get; then install_cmd="apt-get update && apt-get install -y git"
    elif has_cmd dnf;     then install_cmd="dnf install -y git"
    elif has_cmd yum;     then install_cmd="yum install -y git"
    elif has_cmd pacman;  then install_cmd="pacman -S --noconfirm git"
    elif has_cmd apk;     then install_cmd="apk add --no-cache git"
    fi
  fi

  if [[ -z "$install_cmd" ]]; then
    error "Could not determine how to install git."
    info  "Install git manually, then re-run this script."
    exit 1
  fi

  if $needs_privileges; then
    local display_cmd
    display_cmd="$(show_privileged_command "$install_cmd")"
    info "Will run:  ${display_cmd}"
    warn "This requires elevated privileges."
    if ! confirm "Continue?" "yes"; then
      error "Cannot continue without git."
      exit 1
    fi

    run_privileged_command "$install_cmd"
  else
    info "Will run:  ${install_cmd}"
    bash -lc "$install_cmd"
  fi

  if ! has_cmd git; then
    error "git installation failed. You may need to restart your shell."
    exit 1
  fi
  success "git installed."
}

# Destination picker 

ensure_dir_writable() {
  local dir="$1"
  local parent_dir
  parent_dir="$(dirname "$dir")"

  if [[ ! -d "$parent_dir" ]]; then
    mkdir -p "$parent_dir" 2>/dev/null || {
      error "Cannot create directory: $parent_dir"
      info  "Check permissions or choose a different path."
      exit 1
    }
  fi

  if [[ ! -w "$parent_dir" ]]; then
    error "No write permission for: $parent_dir"
    info  "Choose a different path or fix permissions."
    exit 1
  fi
}

choose_destination() {
  # Step 1: Scope
  local scope
  scope=$(choose "Installation scope:" \
    "System-wide — available in all projects" \
    "Current project ($(basename "$(pwd)"))")

  local base_dir
  case "$scope" in
    "System-wide"*)
      base_dir="${HOME}" ;;
    "Current project"*)
      base_dir="$(pwd)" ;;
  esac

  # Step 2: Which agents
  local agents
  agents=$(choose "Which AI agents should have access?" \
    "All of them — recommended" \
    "Codex, Cursor, and others (.agents/)" \
    "Claude Code (.claude/)")

  case "$agents" in
    "All of them"*)
      INSTALL_DIR="${base_dir}/.agents/skills/${SKILL_NAME}"
      EXTRA_DIRS=("${base_dir}/.claude/skills/${SKILL_NAME}") ;;
    "Codex"*)
      INSTALL_DIR="${base_dir}/.agents/skills/${SKILL_NAME}" ;;
    "Claude Code"*)
      INSTALL_DIR="${base_dir}/.claude/skills/${SKILL_NAME}" ;;
  esac

  ensure_dir_writable "$INSTALL_DIR"
  for dir in ${EXTRA_DIRS[@]+"${EXTRA_DIRS[@]}"}; do
    ensure_dir_writable "$dir"
  done
}

# Handle existing installation 

handle_existing() {
  local found=()
  if [[ -d "$INSTALL_DIR" ]]; then
    found+=("$INSTALL_DIR")
  fi
  for dir in ${EXTRA_DIRS[@]+"${EXTRA_DIRS[@]}"}; do
    if [[ -d "$dir" ]]; then
      found+=("$dir")
    fi
  done

  if (( ${#found[@]} == 0 )); then
    return 0
  fi

  echo ""
  warn "Existing installation found at:"
  for dir in "${found[@]}"; do
    info "  $dir"
  done
  echo ""
  info "Your credentials at ~/${CREDENTIALS_DIR#"$HOME"/} are stored separately and will not be affected."
  echo ""

  if confirm "Remove existing installation(s) and reinstall?" "yes"; then
    for dir in "${found[@]}"; do
      rm -rf "$dir"
    done
    success "Previous installation(s) removed."
  else
    info "Installation cancelled."
    exit 0
  fi
}

# Clone 

clone_repo() {
  echo ""

  if $USE_GUM; then
    run_gum style --bold "  Cloning Lighter Agent Kit..." 2>/dev/null \
      || printf "${BOLD}  Cloning Lighter Agent Kit...${RESET}\n"
  else
    info "Cloning Lighter Agent Kit..."
  fi
  git clone --progress --depth 1 --single-branch "$REPO_URL" "$INSTALL_DIR"

  if [[ ! -f "${INSTALL_DIR}/SKILL.md" ]]; then
    error "Clone completed but SKILL.md is missing — the repository may be incomplete."
    info  "Check the repository URL and try again."
    rm -rf "$INSTALL_DIR"
    exit 1
  fi

  success "Cloned to $INSTALL_DIR"
}

# Bootstrap (install SDK dependencies) 

run_bootstrap() {
  local bootstrap="${INSTALL_DIR}/scripts/bootstrap.py"
  if [[ ! -f "$bootstrap" ]]; then
    warn "bootstrap.py not found — skipping automatic dependency setup."
    info "Run manually later:  $PYTHON_CMD ${INSTALL_DIR}/scripts/bootstrap.py"
    return 0
  fi

  echo ""
  local output=""

  if $USE_GUM; then
    output=$(run_gum spin --spinner dot --title "Installing SDK dependencies..." -- \
      "$PYTHON_CMD" "$bootstrap") || true
  else
    info "Installing SDK dependencies..."
    output=$("$PYTHON_CMD" "$bootstrap" 2>&1) || true
  fi

  if echo "$output" | grep -q '"status".*"ok"'; then
    success "SDK ready"
  elif echo "$output" | grep -q '"error"'; then
    local err_detail
    err_detail=$(echo "$output" \
      | grep -o '"error"[[:space:]]*:[[:space:]]*"[^"]*"' \
      | head -1 \
      | sed 's/.*:.*"\([^"]*\)"/\1/') || true
    warn "Bootstrap warning: ${err_detail:-see output above}"
    info "Retry later:  $PYTHON_CMD $bootstrap"
  else
    success "Dependencies installed."
  fi
}

write_credentials_file() {
  local cred_file="$1"
  local api_key="$2"
  local account_index="$3"
  local api_key_index="$4"
  local host="$5"

  mkdir -p "$(dirname "$cred_file")"

  if [[ -e "$cred_file" ]]; then
    chmod 600 "$cred_file"
  else
    (umask 077 && : > "$cred_file")
  fi

  cat > "$cred_file" <<EOF
LIGHTER_API_PRIVATE_KEY=${api_key}
LIGHTER_ACCOUNT_INDEX=${account_index}
LIGHTER_API_KEY_INDEX=${api_key_index}
LIGHTER_HOST=${host}
EOF

  chmod 600 "$cred_file"
}

# Credentials

# Prints "INDEX<TAB>LABEL" lines for each account found, or nothing on failure.
fetch_accounts_by_l1() {
  local l1_address="$1" host="$2"
  local url="${host}/api/v1/accountsByL1Address?l1_address=${l1_address}"
  local raw

  if has_cmd curl; then
    raw=$(curl -fsSL --connect-timeout 10 "$url" 2>/dev/null) || true
  else
    raw=$(wget -qO- --timeout=10 "$url" 2>/dev/null) || true
  fi

  [[ -z "$raw" ]] && return 1

  echo "$raw" | "$PYTHON_CMD" -c "
import sys, json
try:
    data = json.loads(sys.stdin.read())
    for a in data.get('sub_accounts', []):
        label = 'Main Account' if a.get('account_type') == 0 else 'Sub-Account'
        print(str(a['index']) + '\t' + label)
except Exception:
    pass
"
}

setup_credentials() {
  echo ""
  local cred_file="${CREDENTIALS_DIR}/credentials"
  local choice
  if [[ -f "$cred_file" ]]; then
    choice=$(choose "Configure trading credentials?" \
      "Skip — keep existing credentials" \
      "Replace — provide new credentials")
  else
    choice=$(choose "Configure trading credentials?" \
      "Skip — use paper trading for now (no account needed)" \
      "Yes — I have a Lighter account")
  fi

  case "$choice" in
    "Skip — keep existing credentials")
      echo ""
      info "Keeping existing credentials — you're all set."
      return 0 ;;
    "Skip"*)
      echo ""
      info "Paper trading works without credentials — you're all set."
      return 0 ;;
  esac

  echo ""

  # Network
  local network
  network=$(choose "Which network?" \
    "Mainnet (mainnet.zklighter.elliot.ai)" \
    "Testnet (testnet.zklighter.elliot.ai)")

  local host
  case "$network" in
    "Mainnet"*) host="https://mainnet.zklighter.elliot.ai" ;;
    "Testnet"*) host="https://testnet.zklighter.elliot.ai" ;;
  esac

  echo ""

  # L1 address → account index lookup
  local account_index=""
  while [[ -z "$account_index" ]]; do
    local l1_address
    l1_address=$(input_text "Ethereum L1 Address" "0x...")
    l1_address="$(echo "$l1_address" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"

    if [[ -z "$l1_address" ]]; then
      warn "No address entered — skipping credential setup."
      return 0
    fi

    info "Looking up account..."
    local accounts_raw
    accounts_raw=$(fetch_accounts_by_l1 "$l1_address" "$host") || accounts_raw=""

    if [[ -z "$accounts_raw" ]]; then
      warn "No accounts found for that address on this network."
      info  "Make sure you have an account at app.lighter.xyz, then try again."
      if ! confirm "Try a different address?" "yes"; then
        return 0
      fi
      continue
    fi

    local indices=() labels=() options=()
    while IFS=$'\t' read -r idx lbl; do
      indices+=("$idx")
      labels+=("$lbl")
      options+=("${lbl} (index: ${idx})")
    done <<< "$accounts_raw"

    if (( ${#options[@]} == 1 )); then
      account_index="${indices[0]}"
      success "Account found: ${labels[0]} (index: ${account_index})"
    else
      local selected
      selected=$(choose "Select account:" "${options[@]}")
      for i in "${!options[@]}"; do
        if [[ "${options[$i]}" == "$selected" ]]; then
          account_index="${indices[$i]}"
          break
        fi
      done
    fi
  done

  echo ""

  # API key
  info "Get your API private key at: https://app.lighter.xyz/apikeys"
  info "If you have chosen a subaccount, make sure to switch to the correct subaccount before generating an API key."
  echo ""
  local api_key
  api_key=$(input_secret "API Private Key")
  api_key="$(echo "$api_key" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//;s/^["'"'"']//;s/["'"'"']$//')"

  if [[ -z "$api_key" ]]; then
    warn "No key entered — skipping credential setup."
    info "Run ${INSTALL_DIR}/lighter-config to configure later."
    return 0
  fi

  # API key index
  local api_key_index
  api_key_index=$(input_text "API Key Index")
  api_key_index="$(echo "$api_key_index" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  if [[ ! "$api_key_index" =~ ^[0-9]+$ ]] || (( 10#$api_key_index < 4 || 10#$api_key_index > 255 )); then
    warn "Invalid API Key Index — skipping credential setup."
    info "Run ${INSTALL_DIR}/lighter-config to configure later."
    return 0
  fi

  write_credentials_file "$cred_file" "$api_key" "$account_index" "$api_key_index" "$host"
  success "Credentials saved to ${cred_file} (mode 600)"
}

# Verify 

verify_install() {
  local health="${INSTALL_DIR}/scripts/health.py"
  if [[ ! -f "$health" ]]; then
    return 0
  fi

  local output
  output=$("$PYTHON_CMD" "$health" 2>/dev/null) || true
  if echo "$output" | grep -q '"status".*"ok"'; then
    success "Health check passed."
  else
    warn "Health check did not return OK — this may resolve after first use."
  fi
}

# Success screen 

show_success() {
  echo ""

  if $USE_GUM; then
    run_gum style \
      --border rounded \
      --border-foreground 82 \
      --padding "0 2" \
      --bold \
      "✓ Lighter Agent Kit installed!" \
      || success "Lighter Agent Kit installed!"
  else
    echo ""
    success "Lighter Agent Kit installed!"
    echo ""
  fi

  echo ""
  printf "${BOLD}Try these prompts in your AI agent:${RESET}\n"
  echo ""
  printf "  ${DIM}•${RESET} \"Show me the BTC order book\"\n"
  printf "  ${DIM}•${RESET} \"Build a simple momentum strategy for BTC and run it on my paper account\"\n"
  printf "  ${DIM}•${RESET} \"What are the current BTC and ETH perpetual funding rates?\"\n"
  printf "  ${DIM}•${RESET} \"What's my portfolio performance?\"\n"
  echo ""
  printf "${DIM}Installation:  %s${RESET}\n" "$INSTALL_DIR"
  for extra in ${EXTRA_DIRS[@]+"${EXTRA_DIRS[@]}"}; do
    printf "${DIM}  symlinked:   %s${RESET}\n" "$extra"
  done
  printf "${DIM}Credentials:   %s/credentials${RESET}\n" "$CREDENTIALS_DIR"
  echo ""
}

# Main 

main() {
  # Pre-flight
  if ! has_cmd curl && ! has_cmd wget; then
    error "Neither curl nor wget found. Install one and re-run."
    exit 1
  fi

  detect_platform
  install_gum

  header "Lighter Agent Kit - Setup"

  # Python
  if find_python; then
    success "Python found: $("$PYTHON_CMD" --version 2>&1)"
  else
    offer_python_install
  fi

  # Git
  check_git
  success "git found: $(git --version 2>&1 | head -1)"

  # Where to install
  echo ""
  choose_destination

  # Existing installation
  handle_existing

  # Clone
  clone_repo

  # Create symlinks for extra directories
  for extra in ${EXTRA_DIRS[@]+"${EXTRA_DIRS[@]}"}; do
    ln -snf "$INSTALL_DIR" "$extra"
    success "Symlinked $extra → $INSTALL_DIR"
  done

  # Dependencies
  run_bootstrap

  # Credentials (optional)
  setup_credentials

  # Smoke test
  verify_install

  # Done
  show_success
}

main "$@"
