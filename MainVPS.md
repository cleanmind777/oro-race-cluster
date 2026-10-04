# MainVPS.md — set up the MAIN server

The main VPS is the only machine that:

- listens on a port (`BIND_PORT`, default `8080`),
- serves the API and the React dashboard,
- holds the race data (`Data/races/<label>/`),
- decides which small unit of work each machine gets next.

It also downloads race files itself, through its own local worker.

Extra machines are set up with **OtherVPS.md**. They need nothing from this file
except main's address, the port, and the cluster token.

---

## 1. Requirements

| Thing | Why | Note |
| --- | --- | --- |
| Python 3.9+ | `oro_main.py`, `oro_worker.py` | stdlib only, no `pip install` |
| Node 18+ and npm | build the dashboard once | not needed at runtime |
| PM2 | keeps processes alive and restarts them on boot | `npm install -g pm2` |
| ~3 GB free disk | a full race is ~27,000 episode files at ~110 KB | grows with race size |
| Outbound 443 | `api.oroagents.com` | |
| Inbound `BIND_PORT` | workers pull work and upload files | from worker IPs only, or VPN |

Check:

```bash
python3 --version
node --version
npm --version
df -h .
```

---

## 2. Install

```bash
# put the repo on the main VPS, then:
cd oro-race-cluster

npm install -g pm2

# the single config file (edited in section 3)
cp .env.example .env

# build the dashboard once; oro-main serves the result from frontend/dist
cd frontend
npm install
npm run build
cd ..
```

If you skip the build, the API still works and `http://main:8080` shows a short page
telling you to build. The CLI works either way.

---

## 3. Configure `.env`

Everything is configured in one file. Both Python programs and the PM2 files read it, so
nothing needs exporting by hand.

```bash
cp .env.example .env
nano .env
```

The minimum for a main VPS:

```ini
BIND_HOST=0.0.0.0
BIND_PORT=8080

# worker routes: only these IPs may upload into your dataset
ALLOWED_IPS=203.0.113.20, 198.51.100.30

# dashboard: reachable from anywhere, behind a login
UI_USER=admin
UI_PASSWORD=a-long-random-password

CLUSTER_TOKEN=                 # optional second factor for the worker routes
DATA_DIR=./Data/races

MAIN_ORO_MAX_RPM=49            # metadata fetch
ORO_MAX_RPM=49                 # main's local worker; the two share one public IP
```

### Two gates on one port

`oro-main` serves the dashboard and the worker routes on the same port and gates them
separately. A dashboard login grants **no** upload rights; a worker token grants **no**
dashboard access.

| Routes | Gate | Who uses them |
| --- | --- | --- |
| `GET /api/work`, `PUT /api/upload`, `POST /api/work/*` | `ALLOWED_IPS` (+ `CLUSTER_TOKEN`) | your extra VPSs |
| dashboard, `/api/status`, `/api/workers`, `POST /api/races` | `UI_USER` + `UI_PASSWORD` | you, from any IP |
| `GET /api/health` | none | uptime checks |

#### `ALLOWED_IPS` — the worker routes

These are the routes that write into your data, so these are the ones that must not be
open. Entries are IPs or CIDR blocks, comma separated.

- `127.0.0.0/8` and `::1` are **always** allowed, so main's own worker and the CLI work
  without being listed.
- Blocked requests get `403 - <ip> is not in ALLOWED_IPS on this server`, logged at most
  once per IP per minute so a scanner cannot flood your logs.
- **Empty means any IP may upload.** Never leave it empty while the port is public.

Collect each extra VPS's public IP by running this **on that machine**:

```bash
curl -s https://api.ipify.org
```

You do *not* need to list the IP you browse from: the dashboard is gated by the password,
not by IP.

#### `UI_USER` / `UI_PASSWORD` — the dashboard

HTTP Basic auth, so the browser shows its own login dialog and remembers it for the
session. Generate a long password:

```bash
python3 -c 'import secrets;print(secrets.token_urlsafe(24))'
```

> **The password is only base64-encoded in transit.** On a public HTTP port, anyone who
> can watch the network path can read it. If that matters, put main behind the HTTPS
> reverse proxy in section 12, or keep the dashboard on a VPN / SSH tunnel instead of a
> public port. `oro-main` prints a reminder about this at startup.

After `UI_MAX_FAILS` (default 10) wrong passwords from one IP, further wrong guesses get
`429` for `UI_BAN_SEC` (default 300 s). The **correct** password always works, so nobody
can lock you out of your own dashboard by guessing at it.

To restrict the dashboard by IP as well, set `UI_ALLOWED_IPS` in the same format as
`ALLOWED_IPS`. Leave it empty for a public dashboard.

#### `CLUSTER_TOKEN` — optional

A second factor on the worker routes only. With `ALLOWED_IPS` set it adds little; set it
for defence in depth, and then every worker's `.env` needs the same value.

> A variable exported in your shell beats `.env`. If the startup log shows a gate you did
> not configure, `unset` that variable and start again.

### All main-side keys

| Key | Default | Meaning |
| --- | --- | --- |
| `ALLOWED_IPS` | *(empty = everyone)* | IPs/CIDRs allowed on the **worker** routes; loopback always allowed |
| `UI_USER` | *(empty = no login)* | dashboard username |
| `UI_PASSWORD` | *(empty)* | dashboard password |
| `UI_ALLOWED_IPS` | *(empty = any IP)* | optional IP restriction for the dashboard |
| `UI_MAX_FAILS` | `10` | wrong passwords from one IP before `429` |
| `UI_BAN_SEC` | `300` | how long that `429` lasts |
| `CLUSTER_TOKEN` | *(empty = disabled)* | optional shared secret for the worker routes |
| `TRUST_PROXY` | `0` | `1` only behind your own reverse proxy (section 12) |
| `BIND_HOST` | `0.0.0.0` | `127.0.0.1` when proxying, or a VPN address |
| `BIND_PORT` | `8080` | the only listening port in the cluster |
| `DATA_DIR` | `./Data/races` | where race files are written; use another volume if needed |
| `MAIN_ORO_MAX_RPM` | `49` | request cap for main's metadata fetch |
| `ORO_MAX_RPM` | `98` | cap for main's local worker — **set this to `49`** |
| `WORKER_THREADS` | `4` | download threads for main's local worker |
| `UNIT_MAX_EPISODES` | `20` | episodes per work unit |
| `UNIT_STALE_SEC` | `180` | no file progress for this long ⇒ the unit is requeued |
| `PYTHON` | `python3` | interpreter PM2 uses |

Main runs **two** processes against ORO from **one** public IP and the public cap is ~100
requests/min per IP, which is why both RPM values are 49. See section 9.

`.env` is gitignored.

Apply later edits with `pm2 start ecosystem.main.cjs` — PM2 keeps the environment an app
was started with, so a plain `pm2 restart` would quietly reuse the old values. For a
single temporary change, a real environment variable still wins:
`ORO_MAX_RPM=40 pm2 restart oro-worker --update-env`, which lasts until the next
`pm2 start ecosystem.main.cjs`.

---

## 4. Open the firewall

Only main needs an inbound rule. Because the dashboard and the worker routes share one
port, opening it publicly for the UI also exposes the worker routes to the internet —
which is exactly why `ALLOWED_IPS` guards them. Keep it set.

First check what this machine actually has. Many VPS images ship with **no** firewall
package and an empty, accept-everything ruleset, in which case `ufw` and `firewall-cmd`
simply do not exist:

```bash
command -v ufw firewall-cmd          # prints nothing if neither is installed
sudo iptables -S INPUT               # "-P INPUT ACCEPT" with no rules = nothing filtered
ss -lntp                             # what is actually reachable right now
```

If nothing is installed and you want the dashboard public anyway, you can skip this
section: `ALLOWED_IPS` and the dashboard login are doing the work, and `ss -lntp` tells
you main is the only new listener. Otherwise install a firewall:

```bash
# Debian/Ubuntu
sudo apt update && sudo apt install -y ufw
sudo ufw allow 22/tcp                # FIRST, or enabling ufw locks you out of SSH
sudo ufw allow 8080/tcp              # the dashboard + worker port
sudo ufw enable
sudo ufw status verbose

# RHEL/Alma/Rocky
sudo dnf install -y firewalld && sudo systemctl enable --now firewalld
sudo firewall-cmd --permanent --add-service=ssh
sudo firewall-cmd --permanent --add-port=8080/tcp
sudo firewall-cmd --reload
```

If you would rather not expose the port at all, allow it only from your worker IPs and
reach the dashboard through a tunnel:

```bash
sudo ufw allow from 203.0.113.20 to any port 8080 proto tcp
sudo ufw allow from 198.51.100.30 to any port 8080 proto tcp
ssh -L 8080:127.0.0.1:8080 you@main       # then browse http://127.0.0.1:8080
```

> **If Docker runs on this machine**, note that published container ports (`-p`) bypass
> ufw, because Docker writes its own iptables chains. That does not affect `oro-main`,
> which PM2 runs directly on the host, but do not assume ufw hides your containers.

Many providers also have a separate cloud firewall / security group — open the port there
too, or the packets never reach the VPS. On some hosts that panel is the *only* firewall,
and nothing needs installing on the VPS itself.

**Windows main:**

```powershell
# public dashboard
New-NetFirewallRule -DisplayName "oro-main 8080" -Direction Inbound -Protocol TCP `
  -LocalPort 8080 -Action Allow

# or workers only, with the dashboard reached over a tunnel
New-NetFirewallRule -DisplayName "oro-main 8080" -Direction Inbound -Protocol TCP `
  -LocalPort 8080 -RemoteAddress 203.0.113.20,198.51.100.30 -Action Allow
```

**Tailscale / VPN instead of a public port** — nothing is exposed to the internet:

```bash
export BIND_HOST=100.x.y.z      # main's tailscale IP
# workers then use MAIN_URL=http://100.x.y.z:8080
```

---

## 5. Start the processes

No variables to export — `.env` is read by the apps themselves.

```bash
pm2 start ecosystem.main.cjs
pm2 save
pm2 startup          # prints a command; run that command to survive reboots
```

This starts two PM2 apps:

| App | Role |
| --- | --- |
| `oro-main` | API + dashboard on `BIND_HOST:BIND_PORT`, metadata fetch, work queue |
| `oro-worker` | main's own downloader, `WRITE_LOCAL=1`, writes straight to `DATA_DIR` |

`WRITE_LOCAL=1` means main's worker never uploads to itself — it writes files directly.

Verify:

```bash
pm2 status                                  # both online, restarts 0
curl -s localhost:8080/api/health           # {"ok": true, "phase": "idle", ...}
curl -s localhost:8080/api/workers          # main's "local" worker, status "waiting"
pm2 logs oro-main --lines 5 --nostream
```

The expected log lines are:

```
[main 12:00:00] serving on http://0.0.0.0:8080  data=/root/oro-race-cluster/Data/races  ui=dist
[main 12:00:00] access: workers=ip-allowlist(2+loopback)  dashboard=login(admin)
[main 12:00:00] worker IPs: 203.0.113.20/32, 198.51.100.30/32
[main 12:00:00] NOTE: the dashboard login is sent base64-encoded over plain HTTP. ...
```

Read the `access:` line carefully — it is your whole security configuration in one place,
and it is how you confirm your intent matches reality:

| Value | Meaning |
| --- | --- |
| `workers=ip-allowlist(N+loopback)` | only those N entries and loopback may upload |
| `workers=...+token` | they must also send `X-Cluster-Token` |
| `workers=OPEN` | **anyone who can reach the port can write into your data dir** |
| `dashboard=login(admin)` | the browser must authenticate as that user |
| `dashboard=ip-allowlist(N+loopback)+login(...)` | both, if you set `UI_ALLOWED_IPS` |
| `dashboard=OPEN` | **anyone who can reach the port can view progress and start races** |

Either `OPEN` also prints an explicit warning. Fix it in `.env` and run
`pm2 start ecosystem.main.cjs` again. `ui=dist` confirms the dashboard was built.

The worker now **waits**. Starting PM2 does not start a race.

> **Re-running is safe.** `pm2 start ecosystem.main.cjs` replaces both apps rather than
> duplicating them, so that is also how you apply a changed token, port or RPM value.
> Run `pm2 save` again afterwards.

---

## 6. Start a race

Only main starts races. Workers need no command — they pick up work on their own.

### From the dashboard

Open `http://<main-ip>:8080`. The browser asks for the `UI_USER` / `UI_PASSWORD` you set
in `.env` and remembers them until you close it. Then:

1. **race_id** — e.g. `51abddf9-0ea6-4afb-9b19-bd0bdc182af6`
   (from the race page URL, `https://oroagents.com/race/<race_id>`)
2. **label** — optional; the default is `race<race_number>` read from `race.json`,
   so race 2 becomes `race2`
3. **cluster token** — leave empty; it is only needed if you also set `CLUSTER_TOKEN`
4. Press **Start race**

To log out, close the browser (HTTP Basic credentials are cached for the session).

### From the CLI

Run it from the repo directory so it picks up `.env`: that is where it reads `BIND_PORT`
and the credentials it sends (`UI_USER`/`UI_PASSWORD`, plus `CLUSTER_TOKEN` if set). The
CLI connects over loopback, which is always allowed.

```bash
cd ~/oro-race-cluster
python3 oro_main.py start --race-id 51abddf9-0ea6-4afb-9b19-bd0bdc182af6 --label race2
```

```json
{"race_id": "51abddf9-...", "label": "race2", "phase": "metadata"}
```

This posts to the server that is already running under PM2; it does not start a second
one. `409 race already running` means a race is still in progress.

### What happens next

1. **metadata phase** — main reads the race, then each qualifier's runs and each included
   eval run, writing every response unchanged. Episodes are queued as they are discovered,
   so workers start downloading within seconds instead of waiting for the whole scan.
2. **episode phase** — workers pull one small unit at a time until the queue is empty.

Only runs whose `race_id` matches, with `phase == "RACE"`, `is_included == true` and a
status other than `STALE`, are downloaded. `AGENT_CODE` is never requested.

---

## 7. Watch progress

```bash
python3 oro_main.py status            # cluster + per-VPS table in the terminal
pm2 status                            # processes on this machine
pm2 logs oro-worker                   # what main itself is downloading
pm2 logs oro-main                     # race events, 429s, requeues

curl -s localhost:8080/api/status | python3 -m json.tool

find Data/races/race2 -path '*/episodes/*' | wc -l    # episode files landed
du -sh Data/races/race2
```

`oro_main.py status` prints:

```
race   : 51abddf9-...  label=race2  phase=episodes
meta   : agents 158/158  runs 300/300  episodes 27000
files  : done 14230/27000 (52.7%)  pending 12600  in-flight 170  pre-existing 0
rate   : 318.0 files/min  eta 40m05s  429s 4  requeues 0  steals 2
worker                status          done  units flight     epm  429       MB   seen
local                 downloading     4120     210     18    96.1    1    465.2     1s
vps-fra-1             downloading     5300     265     20   112.4    0    598.7     0s
vps-sgp-1             rate-limited    4810     241     20    88.3    3    543.1     2s
```

The dashboard shows the same thing and refreshes every 1.5 s: both phases, cluster
totals, files per minute, ETA, and one row per VPS with its recent EPM, in-flight count,
429 count, bytes landed on main and last-seen age.

Worker statuses: `waiting` (idle, long-polling), `downloading`, `rate-limited` (parked on
an ORO 429, resumes by itself), `offline` (no contact for 90 s).

---

## 8. Data layout on main

```
Data/races/race2/
  race.json
  agents/<agent_version_id>/runs.json
  agents/<agent_version_id>/<eval_run_id>/evaluation_run.json
  agents/<agent_version_id>/<eval_run_id>/episodes/<episode_result_id>.json
```

Every file is the **unmodified API response body**. Writes go to `*.partial` and are then
renamed, so an interrupted download never leaves a half file behind.

This is the only copy in the cluster. Remote machines keep nothing. Back it up yourself
if it matters; `Data/` is gitignored.

---

## 9. Rate limits and main's own IP

The ORO public API allows roughly **100 requests per minute per public IP**. Every process
that talks to ORO holds one limiter shared by all its threads: a minimum gap of 60/98 s
plus a sliding 60-second window, so there is no burst at startup.

On a 429 the process parks **all** its ORO threads for `Retry-After` (default 60) + 15 s,
clears its window, reports the 429 to main, and resumes on its own. You do not need to
intervene; the UI shows the machine as `rate-limited`.

Main is a special case: `oro-main` (metadata) and main's local `oro-worker` are two
processes sharing one public IP, so each is capped at 49/min. Once the metadata phase is
done, hand the whole budget to the worker:

```bash
ORO_MAX_RPM=90 pm2 restart oro-worker --update-env
```

If main's IP also serves other ORO traffic (a validator, another script), lower both
numbers instead of collecting 429s.

---

## 10. Resume

Start the same `race_id` again — from the dashboard or the CLI. Files that already exist
with size > 0 are skipped at three points: when the queue is built, when a unit is handed
out, and on the worker itself. The status line reports them as `pre-existing`, and the
dashboard says *already on disk*.

This is also the correct response to a crash, a reboot, or a stopped race: just start it
again.

---

## 11. Day-to-day

```bash
pm2 restart oro-main                 # restart the server (workers reconnect by themselves)
pm2 restart oro-worker               # restart main's downloader
pm2 stop oro-worker                  # stop main downloading; remote workers continue
pm2 logs --lines 100                 # recent output from both apps
pm2 flush                            # truncate log files
pm2 save                             # persist the current app list for reboot
pm2 resurrect                        # restore it by hand
```

After editing `oro_main.py` or `oro_worker.py`:

```bash
pm2 restart oro-main oro-worker
```

After editing anything in `frontend/`:

```bash
cd frontend && npm run build && cd ..   # no restart needed; files are read per request
```

After editing `.env`, start from the config file again, which re-reads it:

```bash
pm2 start ecosystem.main.cjs           # e.g. after changing ALLOWED_IPS
```

`pm2 restart oro-main` is **not** enough for `.env` changes: PM2 would hand the processes
the environment they were started with.

Changing `BIND_PORT` also means updating `MAIN_URL` on **every** worker and the firewall
rule. Main's own worker follows the new port automatically.

---

## 12. HTTPS (recommended with a public dashboard) and a dev UI

With a public dashboard this is the fix for the cleartext-password problem: terminate
HTTPS on main, so both your login and the workers' traffic are encrypted. Workers then
use `MAIN_URL=https://main.example.com`.

```nginx
server {
    listen 443 ssl;
    server_name main.example.com;
    # ssl_certificate ...;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_read_timeout 120s;       # long-poll is ~60s; must not be cut short
        proxy_request_buffering off;   # uploads stream through
        client_max_body_size 64m;
    }
}
```

Keep `BIND_HOST=127.0.0.1` when proxying, so nothing but the proxy can reach the API.

With a proxy in front, every request arrives from loopback, so `ALLOWED_IPS` would see
only the proxy and would accept any worker. To keep filtering by real client IP, set
`TRUST_PROXY=1` in `.env` and
make sure nginx sends `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;` —
main then judges the last entry of that header, which your proxy appends and a client
cannot forge. Never set `TRUST_PROXY=1` on a directly exposed port.

For UI development only:

```bash
cd frontend && npm run dev     # 127.0.0.1:5173, proxies /api to 8080
```

Never expose 5173 in production. Build the UI and let `oro-main` serve it.

---

## 13. Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| log says `workers=OPEN` | `ALLOWED_IPS` is empty in `.env`; set it and restart |
| log says `dashboard=OPEN` | `UI_USER`/`UI_PASSWORD` are empty; set them and restart |
| browser keeps re-asking for the password | typo in `UI_USER`/`UI_PASSWORD`; the active user is in the `access:` log line |
| `429 too many failed logins` | wrong password tried too often from that IP; the correct password still works immediately |
| the dashboard returns 403 | you set `UI_ALLOWED_IPS` and your IP is not in it; add it, or use `ssh -L 8080:127.0.0.1:8080 main` |
| workers log `403` | that worker's public IP is missing from `ALLOWED_IPS`. Check `pm2 logs oro-main` for `blocked <ip>` |
| a worker's IP changed | update `ALLOWED_IPS`, then `pm2 start ecosystem.main.cjs` |
| an edited `.env` seems ignored | you used `pm2 restart`; run `pm2 start ecosystem.main.cjs` instead |
| main's worker logs `waiting for work from` another host | `MAIN_URL` is set in main's `.env`; comment it out so the local worker follows `BIND_PORT` |
| workers log `401` | main has a `CLUSTER_TOKEN` the worker does not send, or they differ |
| log says `+token` but you wanted none | a `CLUSTER_TOKEN` is exported in the shell that ran `pm2 start`; `unset` it and start again |
| workers log `main unreachable` | firewall or cloud security group, or their `MAIN_URL` is missing the port |
| page says the React build is missing | run `npm run build` in `frontend/` |
| `cannot reach oro-main` from the CLI | `oro-main` is not running (`pm2 logs oro-main`), or `BIND_PORT` is not exported in your shell |
| `409 race already running` | a race is still in the metadata or episode phase |
| many 429s on main | lower `MAIN_ORO_MAX_RPM` and `ORO_MAX_RPM` in `.env`; something else on this IP is calling ORO |
| `oro-main` restart count climbing | read `pm2 logs oro-main`; usually the port is already in use |
| a worker sits at `offline` with work in flight | its unit is requeued automatically after `UNIT_STALE_SEC` (180 s) |
| extra VPS does not speed things up | it shares a public IP with another worker; only **different** public IPs add throughput |
| disk filling up | move the data: `DATA_DIR=/mnt/big/races pm2 start ecosystem.main.cjs && pm2 save` |
