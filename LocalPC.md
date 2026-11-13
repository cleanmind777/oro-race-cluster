# LocalPC.md — home / office PC workers (changing VPN IP)

VPS workers stay on main’s `ALLOWED_IPS` list. A **local PC behind a VPN** often
gets a new public IP every reconnect, so editing the allowlist each time does not
scale.

**Solution (no tunnels):** main accepts a worker if **either**

- its IP is in `ALLOWED_IPS` (stable VPS), **or**
- it sends the shared `CLUSTER_TOKEN` (local / VPN PC).

That is `WORKER_AUTH_MODE=ip_or_token` on main.

The worker still only makes **outbound** HTTP (to main and to `api.oroagents.com`).
Nothing new listens on the PC.

---

## 1. On main (once)

In main’s `.env`:

```ini
# keep every stable VPS IP here
ALLOWED_IPS=203.0.113.20, 198.51.100.30

# shared secret for local/VPN workers (generate a long random string)
CLUSTER_TOKEN=paste-a-long-random-secret-here

# VPS by IP OR local PC by token
WORKER_AUTH_MODE=ip_or_token
```

Generate a token:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Apply (PM2 keeps the old env on a plain restart):

```bash
pm2 start ecosystem.main.cjs
pm2 save
```

Startup log should mention `WORKER_AUTH_MODE=ip_or_token` and `token-or`.

VPS workers do **not** need the token in their `.env` while their IP stays listed.
Local PCs **must** set `CLUSTER_TOKEN` to the same value.

---

## 2. On each local PC

Copy the same files as any extra worker (`OtherVPS.md` §2). Prefer a short unique
`WORKER_ID`.

Linux / macOS `.env`:

```ini
MAIN_URL=http://203.0.113.10:8080
WORKER_ID=home-laptop-1
WORKER_THREADS=4
ORO_MAX_RPM=98
CLUSTER_TOKEN=paste-the-same-secret-as-main
WRITE_LOCAL=0
```

Windows: same keys, plus `python -m pip install --upgrade certifi` (see
`OtherVPS.md` §4) so HTTPS to ORO verifies.

Check reachability (token is not required for `/api/health`):

```bash
curl -s http://203.0.113.10:8080/api/health
```

Start the worker as in `OtherVPS.md` (PM2 on Linux/Windows). It should appear on
the dashboard even when `curl -s https://api.ipify.org` changes after a VPN
reconnect — no main restart needed.

---

## 3. Firewall / rate limits

- Main’s `BIND_PORT` (8080) must still accept TCP from the PC (or use Tailscale /
  SSH and point `MAIN_URL` there — optional, not required for this mode).
- ORO’s cap is **per public IP**. Several home PCs on the **same** VPN egress share
  one budget; different VPN exits help more than more processes on one IP.

---

## 4. Modes reference

| `WORKER_AUTH_MODE` | Meaning |
| --- | --- |
| `ip_and_token` | Default. Allowlist (if set) **and** token (if set). Old behaviour. |
| `ip_or_token` | Allowlist **or** token. Use for mixed VPS + local/VPN PCs. |
| `ip` | Allowlist only. |
| `token` | Token only (any IP with the secret). |

---

## 5. Troubleshooting

| Symptom | Fix |
| --- | --- |
| Local PC: `401` / missing token | Put `CLUSTER_TOKEN` in the PC’s `.env`, recreate/restart the worker |
| Local PC: `401` bad token | Typo or main still on an old env — `pm2 start ecosystem.main.cjs` on main |
| Local PC: `403` not in ALLOWED_IPS | Main is not in `ip_or_token`, or `CLUSTER_TOKEN` empty on main |
| VPS suddenly needs a token | You set `ip_and_token` with a token, or removed its IP from `ALLOWED_IPS` |
| VPN IP changed, still works | Expected with `ip_or_token` + token on the PC |
