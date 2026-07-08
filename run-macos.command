#!/bin/bash
# Removes the Gatekeeper quarantine flag macOS adds to anything downloaded
# from a browser (this build isn't code-signed with an Apple Developer
# certificate, so Gatekeeper would otherwise refuse to run it), then
# launches Russgeist. Double-click this file in Finder to run it.
cd "$(dirname "$0")"

BINARY=""
for name in Russgeist-macos-arm64 Russgeist-macos-intel Russgeist; do
  if [ -f "$name" ]; then
    BINARY="$name"
    break
  fi
done

if [ -z "$BINARY" ]; then
  echo "Could not find a Russgeist binary next to this script."
  read -p "Press Enter to close..."
  exit 1
fi

chmod +x "$BINARY"
xattr -dr com.apple.quarantine "$BINARY" 2>/dev/null

exec "./$BINARY"
