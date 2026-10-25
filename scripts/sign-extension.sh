#!/bin/sh
# Signeert de Firefox-extensie als unlisted via AMO; .xpi komt in web-ext-artifacts/.
# Credentials: https://addons.mozilla.org/developers/addon/api/key/ in
# ~/.config/superbuy-search/amo.env (WEB_EXT_API_KEY=..., WEB_EXT_API_SECRET=...).
# Bij elke wijziging eerst "version" in extension/manifest.json ophogen.
set -e
cd "$(dirname "$0")/.."
set -a; . "${XDG_CONFIG_HOME:-$HOME/.config}/superbuy-search/amo.env"; set +a
npx --yes web-ext sign --channel unlisted --source-dir extension --artifacts-dir web-ext-artifacts
ls -t web-ext-artifacts/*.xpi | head -1
