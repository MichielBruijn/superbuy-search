#!/bin/sh
# Nieuwe extensieversie uitbrengen: signeren, updates.json bijwerken, GitHub-release met .xpi.
# Eerst "version" in extension/manifest.json ophogen. Firefox haalt de update daarna zelf op.
set -e
cd "$(dirname "$0")/.."
VERSION=$(python3 -c "import json; print(json.load(open('extension/manifest.json'))['version'])")
ID=$(python3 -c "import json; print(json.load(open('extension/manifest.json'))['browser_specific_settings']['gecko']['id'])")
XPI="superbuy-search-$VERSION.xpi"
REPO=MichielBruijn/superbuy-search

./scripts/sign-extension.sh
cp "$(ls -t web-ext-artifacts/*-"$VERSION".xpi | head -1)" "web-ext-artifacts/$XPI"
HASH=$(sha256sum "web-ext-artifacts/$XPI" | cut -d' ' -f1)

cat > updates.json <<JSON
{
  "addons": {
    "$ID": {
      "updates": [
        {
          "version": "$VERSION",
          "update_link": "https://github.com/$REPO/releases/download/v$VERSION/$XPI",
          "update_hash": "sha256:$HASH"
        }
      ]
    }
  }
}
JSON

# release eerst, dan updates.json pushen: anders wijst de update naar een nog niet bestaand bestand
gh release create "v$VERSION" "web-ext-artifacts/$XPI" --repo "$REPO" --title "Extensie $VERSION" \
  --notes "Gesigneerde Firefox-extensie. Installeren: about:addons → tandwiel → Add-on installeren via bestand. Bestaande installaties updaten automatisch."
git add updates.json extension/manifest.json
git commit -m "Extensie $VERSION"
git push
