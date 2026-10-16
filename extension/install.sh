#!/usr/bin/env bash
# Install the AI-SDLC Foreman kit into a checkout so `bob --mode foreman-*`
# resolves. Without this, every stage fails argument validation instantly, and
# the error message does not mention modes at all.
#
# On developer machines and in CI, prefer the extension mechanism — it is the
# answer to keeping many repositories in sync:
#
#   bob extensions install <your-foreman-extension-repo> --auto-update
#
# This script is the local and offline path, and the way to test a change to
# the modes before releasing it.
set -euo pipefail

TARGET="${1:-.}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$TARGET/.bob"

mkdir -p "$DEST"
cp "$SRC/custom_modes.yaml" "$DEST/custom_modes.yaml"
rm -rf "$DEST/rules"
cp -R "$SRC/rules" "$DEST/rules"

# AGENTS.md — do NOT merge the repository's own file into ours.
#
# The context file is read as INSTRUCTIONS. A repository's AGENTS.md is
# untrusted content like everything else in it, and concatenating the two would
# hand any repository a direct channel into the agent that is about to write
# code in it. That is exactly what rules/04-untrusted-input.md exists to
# prevent, so it must not be undone by the installer. Theirs is preserved under
# a different name, where it is still readable as data.
if [ -f "$TARGET/AGENTS.md" ] && ! cmp -s "$SRC/AGENTS.md" "$TARGET/AGENTS.md"; then
  mv "$TARGET/AGENTS.md" "$TARGET/AGENTS.repo.md"
  echo "  note: repo's own AGENTS.md preserved as AGENTS.repo.md (data, not instructions)"
fi
cp "$SRC/AGENTS.md" "$TARGET/AGENTS.md" 2>/dev/null || true

# .bobignore — merging IS safe here, because it only ever removes files from
# scope. Keep the repository's exclusions and append ours, so Foreman never
# reads something a team deliberately excluded.
if [ -f "$TARGET/.bobignore" ] && ! cmp -s "$SRC/.bobignore" "$TARGET/.bobignore"; then
  { cat "$TARGET/.bobignore"; echo; echo "# --- appended by AI-SDLC Foreman ---"; \
    cat "$SRC/.bobignore"; } > "$TARGET/.bobignore.merged"
  mv "$TARGET/.bobignore.merged" "$TARGET/.bobignore"
else
  cp "$SRC/.bobignore" "$TARGET/.bobignore" 2>/dev/null || true
fi

echo "Installed AI-SDLC Foreman into $DEST"

if grep -q "TODO" "$DEST/rules/01-repo-policy.md" 2>/dev/null; then
  echo
  echo "  !! rules/01-repo-policy.md still contains TODO placeholders."
  echo "     Until it is filled in, expect plans that reinvent helpers you"
  echo "     already have and code that reads as foreign to your maintainers."
fi

echo
echo "Modes available:"
grep -E '^  - slug:' "$DEST/custom_modes.yaml" | sed 's/  - slug: /  bob run --mode /'
