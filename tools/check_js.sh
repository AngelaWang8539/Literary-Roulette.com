#!/bin/sh
# Syntax-check every browser script before it can reach the site. Run it before every commit:  sh tools/check_js.sh
set -e
tmp="$(mktemp -d)"
bad=0
for f in static/js/*.js; do
  if grep -qE '^(import|export) ' "$f"; then      # ES modules (app.js, sharecard.js ...): check as a module
    cp "$f" "$tmp/check.mjs"; target="$tmp/check.mjs"
  else                                             # classic scripts (i18n.js ...): check as a plain script
    target="$f"
  fi
  if node --check "$target" 2>"$tmp/err"; then echo "ok   $f"; else echo "FAIL $f"; sed -n '1,6p' "$tmp/err"; bad=1; fi
done
rm -rf "$tmp"
exit $bad
