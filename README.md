# superbuy-search

Slimmer zoeken op [Superbuy](https://www.superbuy.com) (Taobao, 1688, Xianyu, Weidian), met Claude als zoekhulp.

Superbuy laat Chinese titels door Google vertalen. Daardoor is zoeken vaak een gok. Hier vertaalt Claude
je wens naar goede Chinese zoektermen (Taobao-jargon, synoniemen), beoordeelt de resultaten op de
**originele Chinese titel** en zet een selectie op een lokale resultatenpagina.

```
Claude Code ──MCP──▶ superbuy-mcp ──HTTP──▶ bridge (127.0.0.1:8765) ◀──long-poll── Firefox-extensie
                                               │                                     │
                                         resultatenpagina                 fetch in je Superbuy-tab
                                                                         (jouw cookies/captcha's)
```

- **extension/**: Firefox-extensie (MV2). Voert zoekopdrachten uit binnen een superbuy.com-tab, als de
  pagina zelf. Cloudflare-checks en inloggen doe je gewoon zelf. Logt Superbuy-API-verkeer passief mee
  naar de bridge, zodat nieuwe endpoints te vinden zijn.
- **bridge**: Starlette-server; opdrachtenwachtrij, captures, sessies en de UI op http://127.0.0.1:8765.
  Alleen `https://*.superbuy.com` mag worden opgevraagd, en elke API-call vereist een custom header,
  zodat andere websites hem niet kunnen misbruiken.
- **superbuy-mcp**: tools `status`, `search`, `publish`, `superbuy_fetch`, `superbuy_captures`.

## Installatie

```sh
uv sync
cp systemd/superbuy-bridge.service ~/.config/systemd/user/
systemctl --user enable --now superbuy-bridge
claude mcp add -s user superbuy -- uv run --project ~/Projects/superbuy-search superbuy-mcp
```

Extensie laden: `about:debugging#/runtime/this-firefox` → *Tijdelijke add-on laden* →
`extension/manifest.json`. Een tijdelijke add-on verdwijnt bij het herstarten van Firefox. Voor
permanent gebruik: signeren als unlisted via AMO (`web-ext sign --channel unlisted`).

Data: `~/.local/share/superbuy-search/` (sessies, item-cache, captures).
