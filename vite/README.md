# knfapp-vite — the admin panel

The administrators' web console of the knfapp stack: a React SPA (Vite
build, MUI + Tailwind) served as static files behind the Caddy ingress
under **`/adminpanel/`**.

What it covers:

- **Dashboard** — system counters, open moderation queue, scraper health
- **Users** — roles, deactivation, GDPR erasure (admin)
- **Invitations** — mint / inspect / revoke codes (admin + curator)
- **Moderation** — the reports queue, resolve/reopen, post and chat-message
  previews of the reported targets (admin + curator)
- **Push messages** — broadcast to the admin channel with live job status (admin)
- **Action log** — the admin_audit trail: who wielded the console (admin)
- **News** — the whole feed incl. drafts, publish faculty posts, delete/moderate
- **Memes** — the sticker library wall with search and delete
- **Files** — the stored-file ledger incl. ownerless rows, open/delete (admin)
- **Deleted sources** — the scraper skip-list with the audited restore (admin)
- **Timetable / Faculty info** — read-only views of what the app serves
- **Scrapers** — trigger runs by hand, run history, news yield chart (admin)
- **Wayfind** — buildings, draft vs published revisions, publish, version history
- **Account** — password change, log out everywhere

Signing in uses the same accounts as the mobile app (`POST
/api/auth/login`); only `admin` and `curator` roles are admitted. The
bearer token is attached to every `/api/*` call — the panel itself is
just static files, all authorization happens in the backend.

## Layout

```
vite/
├── Dockerfile        # prod: node build → caddy static serve
├── Dockerfile.dev    # dev: vite dev server with live reload
├── Caddyfile         # in-image SPA server (strips /adminpanel, index.html fallback)
└── app/              # the Vite project (src/, package.json, ...)
```

## Development

Flip the `# Dev` toggles on the `knfapp-vite` service in
docker-compose.yml (Dockerfile.dev + the `./vite/app:/app` volume + the
external network for npm), then `docker compose up -d --build knfapp-vite`.
The dev server serves through the same `/adminpanel/` prefix.

Lint inside the container: `npm run lint`.
