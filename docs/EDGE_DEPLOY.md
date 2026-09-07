# Putting the submission page behind the dashboard's login

The submission page works two ways, and the difference is who gets recorded:

| reached | identity | recorded as |
|---|---|---|
| directly, `http://100.64.254.6:8012/ui` | none — no login on that port | a typed-in label, `*_verified: false` |
| through the edge, `/bambu/ui/` | the dashboard's `ac_auth` session | the signed-in account, `*_verified: true` |

Only the second is attributable, and it is also the only one that can ever have
single sign-on: a session cookie cannot be shared with this gateway on its own
address, because raw `100.x` addresses cannot carry a `Domain` cookie and
`*.ts.net` is on the Public Suffix List. See
`ac-organic-lab/docs/AUTH_DESIGN.md`.

This is the runbook for turning the second one on. Steps 1–3 are safe in any
order; **step 4 must come last**.

Everything here fails closed. Until the secrets on both sides match, the
gateway trusts no injected identity and behaves exactly as it does today.

---

## 0. The secret

Already generated and stored in this repo's gitignored `.env` as
`BAMBU_EDGE_SHARED_SECRET`. Read it back rather than retyping it:

```bash
grep '^BAMBU_EDGE_SHARED_SECRET=' /home/sdl2/caoyang/bambu-server/.env
```

To rotate it instead, generate a new one and update **both** sides together:

```bash
openssl rand -hex 32
```

## 1. Give Caddy the same secret

The edge reads its secrets from an environment file supplied by a systemd
drop-in (`/etc/systemd/system/caddy.service.d/edge-secret.conf`, which points at
`/etc/caddy/edge-secrets.env`). Append the line — same value as `.env`:

```bash
sudo sh -c 'printf "BAMBU_EDGE_SHARED_SECRET=%s\n" "$1" >> /etc/caddy/edge-secrets.env' _ \
  "$(sed -n 's/^BAMBU_EDGE_SHARED_SECRET=//p' /home/sdl2/caoyang/bambu-server/.env)"
```

Check it landed exactly once, without printing it:

```bash
sudo grep -c '^BAMBU_EDGE_SHARED_SECRET=' /etc/caddy/edge-secrets.env   # expect 1
```

## 2. Install the edge route

> **Do not `cp` the repo Caddyfile over the deployed one.** As of 2026-09-07
> `/etc/caddy/Caddyfile` has diverged from
> `ac-organic-lab/deploy/Caddyfile.single-edge`: production factored its routes
> into a shared `(edge_routes)` snippet imported by **two** site blocks (an
> `http://` address and an `https://` MagicDNS one, added when TLS was turned
> on), while the repo copy still has the older single-`http://` layout. Copying
> the repo file over it would silently drop the HTTPS site. Reconciling the two
> is its own task — see `docs/TODO.md`.

So apply the block *into* the deployed file instead. It is the section between
the `# ---- Bambu printer gateway submission page` banner and the next `# ----`
banner in the repo copy, and it belongs inside `(edge_routes)`, immediately
before the `# ---- AnaliticaDB results catalog` banner.

Build the merged file, diff it, then install:

```bash
cd /home/sdl2/caoyang/ac-organic-lab
python3 - <<'EOF'
import pathlib
repo = pathlib.Path("deploy/Caddyfile.single-edge").read_text().split("\n")
live = pathlib.Path("/etc/caddy/Caddyfile").read_text().split("\n")
start = next(i for i, l in enumerate(repo) if "Bambu printer gateway submission page" in l)
end   = next(i for i, l in enumerate(repo) if "AnaliticaDB results catalog" in l and i > start)
assert not any("bambu" in l.lower() for l in live), "already installed"
anchor = next(i for i, l in enumerate(live) if "AnaliticaDB results catalog" in l)
pathlib.Path("/tmp/Caddyfile.new").write_text("\n".join(live[:anchor] + repo[start:end] + live[anchor:]))
print("wrote /tmp/Caddyfile.new")
EOF

# Purely additive? Expect only additions, none removed.
diff /etc/caddy/Caddyfile /tmp/Caddyfile.new | grep -c '^<'   # expect 0

# Syntax check. `caddy validate` also provisions, which needs to read the TLS
# certs — run it under sudo, or use `adapt` (parse only) as this user.
sudo caddy validate --config /tmp/Caddyfile.new --adapter caddyfile

sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak-$(date +%F)   # keep a way back
sudo cp /tmp/Caddyfile.new /etc/caddy/Caddyfile
sudo systemctl reload caddy          # reload, not restart: no dropped connections
systemctl is-active caddy
```

If the reload fails, Caddy keeps running the old config — fix and retry rather
than restarting. If it succeeded but something is wrong, the backup above is the
way back.

## 3. Restart the gateway so it reads the secret

```bash
sudo systemctl restart bambu-server
sc_state=$(systemctl is-active bambu-server); echo "bambu-server: $sc_state"
```

### Verify

The gateway must now *decline* to trust an unaccompanied header, and accept one
that comes through the edge:

```bash
# Direct, forged header -> not verified. This is the important one.
curl -fsS -H 'X-Auth-User: admin' http://127.0.0.1:8012/whoami
# expect: {"user":null,"role":null,"verified":false,"identity_available":true}

# `identity_available: true` confirms the gateway picked the secret up at all.
```

Then open the dashboard, go to **Utils → 3D Printers**, and confirm the framed
panel loads and shows *Signed in as <you>* instead of a name field. The
dashboard needs a rebuild only if its own code changed:

```bash
cd /home/sdl2/caoyang/ac-organic-lab/web && npm run build
sudo systemctl restart ac-organic-lab-web
```

> Check `git status` there first — a build ships whatever is in the working
> tree, including anyone else's in-flight changes.

## 4. Last: close the direct path

Only once `/bambu/ui/` works. This narrows the gateway back to loopback, so
`POST /submissions` is no longer reachable unauthenticated from the tailnet.

Edit `ExecStart` in `deploy/bambu-server.local.service` from `--host 0.0.0.0`
back to `--host 127.0.0.1`, then:

```bash
cd /home/sdl2/caoyang/bambu-server
sudo cp deploy/bambu-server.local.service /etc/systemd/system/bambu-server.service
sudo systemctl daemon-reload && sudo systemctl restart bambu-server
```

The aggregator is unaffected either way — `equipment.yaml` polls
`127.0.0.1:8012`, and the edge proxies over loopback too.

Afterwards the direct URL stops working, so remove or relabel the *Open
directly* fallback link in `ac-organic-lab`'s
`web/src/app/utils/printers/BambuPrinterPanel.tsx`.

---

## If the framed panel is blank

In order of likelihood:

1. **Route not installed** — `curl -sI http://100.64.254.6/bambu/ui/` should
   redirect or return 200, not the dashboard's 404.
2. **Not signed in** — the edge's `forward_auth` returns 401 and the frame shows
   nothing. Log into the dashboard first.
3. **Prefix leak** — if the page loads but its data does not, check the browser
   console for requests to `/printers` instead of `/bambu/printers`. The page
   derives its base by stripping a trailing `/ui`, so it must be reached at
   `/bambu/ui/` (the edge redirects `/bambu` and `/bambu/` there).
4. **Secret mismatch** — the page loads and works but still shows a name field.
   `/bambu/whoami` will report `verified: false`. Compare the two values.
