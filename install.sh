#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
TARGET="$BIN_DIR/orchagent"

if [ -e "$TARGET" ] || [ -L "$TARGET" ]; then
  CURRENT="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$TARGET")"
  EXPECTED="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$ROOT/bin/orchagent")"
  if [ "$CURRENT" != "$EXPECTED" ]; then
    echo "refuse to overwrite existing command: $TARGET -> $CURRENT" >&2
    exit 1
  fi
fi

mkdir -p "$BIN_DIR"
python3 "$ROOT/bin/orchagent" install --link-bin "$TARGET"

cat <<MSG
orchAgent installed.

Next steps:
  export PATH="$BIN_DIR:\$PATH"   # if orchagent is not found
  orchagent doctor
  orchagent config validate
  orchagent extensions list
  orchagent opencode doctor

To register orchAgent with opencode:
  orchagent opencode link
MSG
