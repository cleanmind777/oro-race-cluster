# ORO race-log cluster

Downloads **raw** public ORO race (and qualifying) logs onto one MAIN VPS, using any
number of extra machines as helpers — VPS or local/VPN PCs. Extra machines wait under
PM2, **pull** small units of work from main, and upload each file to main the moment it
is downloaded. Main downloads too. Everything is driven from a React dashboard on main.

```
  extra VPS / local PC (no open port)      MAIN VPS (one port, 8080)
  ┌──────────────────────┐                 ┌─────────────────────────────┐
  │ oro-worker (PM2)     │  GET /api/work  │ oro-main (PM2)              │
  │   long-poll for work │ ──────────────► │   API + React dist/         │
  │   GET ORO :443       │                 │   dynamic work queue        │
  │   PUT /api/upload    │ ──────────────► │   Data/races/<label>/  ◄── the only copy
  └──────────────────────┘                 │ oro-worker (PM2, local)     │
                                           └─────────────────────────────┘
```

**Step-by-step setup:** [MainVPS.md](MainVPS.md) for the main server,
[OtherVPS.md](OtherVPS.md) for each extra Linux/Windows VPS,
[LocalPC.md](LocalPC.md) for home/office PCs whose VPN public IP changes.
This file is the reference for how the whole thing works.

Why a cluster: `api.oroagents.com` allows roughly **100 requests/min per public IP**, and
a full race is ~300 included eval runs × ~90 episodes. One IP is too slow. Extra machines
only help if they have **different public IPs**.

## Files

| File | Role |
| --- | --- |
| `oro_main.py` | main VPS: API, React dist, metadata fetch, work queue, CLI |
| `oro_worker.py` | every VPS incl. main: pulls units, downloads from ORO, uploads/writes |
| `frontend/` | Vite + React dashboard |
| `ecosystem.main.cjs` | PM2: `oro-main` + main's local worker |
| `ecosystem.worker.cjs` | PM2: `oro-worker` on each extra VPS |
| `pm2_start.cjs` | makes `pm2 start ecosystem.*.cjs` work (see note below) |
| `.env.example` | template for `.env`, the single config file |
| `LocalPC.md` | home/VPN workers via `WORKER_AUTH_MODE=ip_or_token` |

> PM2 only auto-detects config files named `*.config.cjs`, `*.json` or `*.yaml`, so
> `pm2 start ecosystem.main.cjs` would otherwise run the config as a script instead of
> reading its `apps`. The last line of each ecosystem file calls `pm2_start.cjs`, which
> notices that case, starts the same app definitions properly, and removes the stray
> wrapper entry. Edit only the ecosystem files; app definitions live there.

## Install

Both Python files are stdlib only — no pip packages on Linux. On **Windows**, also
install a CA bundle so HTTPS to ORO verifies:

```bash
# every machine
python3 --version            # 3.9+
npm install -g pm2
cp .env.example .env         # then edit it

# Windows workers (UCRT64 / PowerShell)
python -m pip install --upgrade certifi

# main only (build the UI once; oro-main serves it)
cd frontend && npm install && npm run build && cd ..
```

Windows: same files, same commands. Use `python` instead of `python3` if that is what is
on PATH, by putting `PYTHON=python` in `.env`. The worker loads `certifi` (or
`SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE`) automatically.

## Configure

All settings live in **`.env`** next to the scripts. Both Python programs and the PM2
files read it, so nothing has to be exported by hand.

```bash
cp .env.example .env
nano .env
```

Apply later edits with `pm2 start ecosystem.main.cjs` (or `ecosystem.worker.cjs`): PM2
keeps the environment an app was started with, so a plain `pm2 restart` reuses the old
values. A real environment variable still wins for a one-off,
`ORO_MAX_RPM=40 pm2 restart oro-worker --update-env`, until the next start from the file.

| Key | Where | Default | Notes |
| --- | --- | --- | --- |
| `ALLOWED_IPS` | main | *(empty = everyone)* | IPs/CIDRs allowed on the **worker** routes; loopback always allowed |
| `UI_USER` / `UI_PASSWORD` | main | *(empty = no login)* | dashboard login (HTTP Basic) |
| `UI_ALLOWED_IPS` | main | *(empty = any IP)* | optional extra IP restriction for the dashboard |
| `UI_MAX_FAILS` / `UI_BAN_SEC` | main | `10` / `300` | wrong-password brake; the correct password always works |
| `CLUSTER_TOKEN` | all | *(empty = disabled)* | shared secret for **worker** routes; required for local/VPN PCs with `ip_or_token` |
| `WORKER_AUTH_MODE` | main | `ip_and_token` | `ip_and_token` / `ip_or_token` / `ip` / `token` — see [LocalPC.md](LocalPC.md) |
| `TRUST_PROXY` | main | `0` | `1` only behind your own reverse proxy |
| `BIND_HOST` | main | `0.0.0.0` | `127.0.0.1` when proxying, or a VPN address |
| `BIND_PORT` | main | `8080` | the only listening port in the cluster |
| `DATA_DIR` | main | `./Data/races` | the only durable copy |
| `QUALIFYING_MIN_SCORE` | main | `0.55` | min `final_score` for qualifying downloads |
| `UPDATE_TASK_DICTIONARY` | main | `1` | rebuild `task_dictionary.json` after a download finishes |
| `PUSH_ORO_LOGS` | main | `1` | staged git commit/push of the finished label under `ORO_LOGS_GIT_DIR` |
| `ORO_LOGS_GIT_DIR` | main | next to the dictionary script | git repo for staged pushes |
| `MAIN_ORO_MAX_RPM` | main | `49` | cap for main's metadata fetch |
| `MAIN_URL` | extra VPS / PC | `http://127.0.0.1:<BIND_PORT>` | **must include the port** |
| `WORKER_ID` | workers | hostname | unique per machine |
| `WORKER_THREADS` | workers | `4` | ORO threads, all under one limiter |
| `ORO_MAX_RPM` | workers | `98` | per **process** cap toward ORO; use `49` on main |
| `WRITE_LOCAL` | set by PM2 | `0` | `1` for main's worker: write to disk, don't upload |
| `AUTO_RESUME` | main | `1` | continue an interrupted race when main starts again |
| `WATCH_INTERVAL_SEC` | main | `60` | how often to check whether a race is published yet |
| `WATCH_REQUIRE_COMPLETE` | main | `1` | also wait for `race.status == RACE_COMPLETE` |
| `PROBE_EPISODES` | main | `3` | episodes tried per check; one success means published |
| `MAX_ITEM_FAILURES` | main | `3` | attempts before a file is given up as unavailable |
| `UNIT_MAX_EPISODES` | main | `20` | episodes per work unit |
| `UNIT_MAX_META` | main | `4` | metadata files per work unit |
| `UNIT_STALE_SEC` | main | `180` | no progress for this long ⇒ requeue the unit |
| `PYTHON` | all | `python3` | set to `python` on Windows |

Main's local worker needs no `MAIN_URL`: it follows `BIND_PORT` automatically.

## Start

**Main VPS** — set `ALLOWED_IPS`, `UI_USER` and `UI_PASSWORD` in `.env`, then:

```bash
pm2 start ecosystem.main.cjs && pm2 save && pm2 startup   # run the line pm2 prints
```

That starts two processes: `oro-main` (the only listener) and `oro-worker` with
`WRITE_LOCAL=1`, which writes straight to `Data/races/` and never uploads to itself.
The startup log states the access policy it ended up with:

```
serving on http://0.0.0.0:8080  data=...  ui=dist
access: workers=ip-allowlist(2+loopback)+token-or  dashboard=login(admin)  WORKER_AUTH_MODE=ip_or_token
worker IPs: 203.0.113.20/32, 198.51.100.30/32
```

**Every extra VPS** — copy `oro_worker.py`, `ecosystem.worker.cjs`, `pm2_start.cjs` and
a `.env` holding `MAIN_URL` and `WORKER_ID`, then:

```bash
pm2 start ecosystem.worker.cjs && pm2 save && pm2 startup
```

**Local / VPN PC** — same worker files; set `CLUSTER_TOKEN` to match main and use
`WORKER_AUTH_MODE=ip_or_token` on main so a changing VPN egress IP does not need an
allowlist edit. Details: [LocalPC.md](LocalPC.md).

Workers now **wait**. Starting PM2 does not start a race — they long-poll `/api/work`
and show up in the UI as `waiting`. If `pm2 startup` is unreliable (common on Windows),
either use `pm2 resurrect` from a login/startup script or register
`pm2 resurrect` as a Task Scheduler task at boot.

## Start a race

Only on main. Workers need no command.

**UI** — open `http://<main-ip>:8080`. The browser asks for `UI_USER` / `UI_PASSWORD`,
then you paste the `race_id`, optionally a label (default is `race<race_number>` from
`race.json`), and press **Start race**. The cluster-token field stays empty unless you
also set `CLUSTER_TOKEN` (that field is for the *browser* talking to worker-gated
routes; local workers put the token in their own `.env`).

**CLI**

```bash
python3 oro_main.py start --race-id 51abddf9-0ea6-4afb-9b19-bd0bdc182af6 --label race2
```

Both work while `oro-main` is already serving; the CLI just POSTs to it.

### Qualifying logs (leaderboard filter)

To download **QUALIFYING** episode logs for public-leaderboard agents with
`final_score >= QUALIFYING_MIN_SCORE` (default `0.55`):

- **UI** — **Start qualifying** (min score + label, default label `qualifying`)
- **CLI** — `python3 oro_main.py start-qualifying [--min-score 0.55] [--label qualifying]`
- **API** — `POST /api/qualifying` with optional `{min_score, label}`

Same cluster path as a race (`race_id` is the synthetic id `qualifying`). Main pages the
public leaderboard, writes `leaderboard_selected.json`, then enqueues each agent's
`runs.json` and only keeps runs with `phase == "QUALIFYING"`. There is no publication
watcher.

### A race whose logs are not public yet

ORO publishes episode logs some hours after a race ends, and the race metadata can still
change in the meantime. You do not have to wait for that or watch for it: start the race
straight away and main enters the **waiting** phase, where it

1. re-reads `race.json` every `WATCH_INTERVAL_SEC` (default 60s) and, with
   `WATCH_REQUIRE_COMPLETE=1`, waits for `status == "RACE_COMPLETE"`;
2. then probes up to `PROBE_EPISODES` real episodes — while they answer
   `404 ARTIFACT_NOT_FOUND` the logs are not published yet (one success is enough, so a
   single missing artifact cannot block the start);
3. starts the cluster by itself as soon as a probe succeeds.

Nothing is downloaded while it waits, and the dashboard shows the reason, the number of
checks and how long it has been waiting. Because the metadata may have changed while
waiting, every metadata file is then fetched **again** rather than trusted from disk, so
you cannot end up with the shorter episode list the race had before it was published.
A restart during the waiting phase resumes waiting.

## Watch

```bash
pm2 status                 # processes alive on this machine
pm2 logs oro-worker        # what this machine is downloading
python3 oro_main.py status # cluster table in the terminal
python3 oro_main.py stop   # stop the race; downloaded files are kept
curl -s localhost:8080/api/status | python3 -m json.tool
find Data/races/race2 -path '*/episodes/*' | wc -l   # episode files on main
```

The UI auto-refreshes every 1.5s: the waiting phase if the logs are not public yet, then
the metadata phase (with its own file counter), then the episode phase, cluster
done/remaining/in-flight, files per minute, ETA, and a row per VPS with its recent EPM,
in-flight count, 429 count, bytes landed on main and last-seen age.

**Pause / resume workers** from the dashboard (per row, or Pause all / Resume all). That
only stops **new job assignment** on main; the worker process keeps polling. In-flight
units may still finish. Paused ids are stored in `.oro-worker-control.json` and survive
a main restart. Status shows `paused`.

## What gets downloaded

```
Data/races/<label>/
  race.json                         # races only
  leaderboard_selected.json         # qualifying only
  agents/<agent_version_id>/runs.json
  agents/<agent_version_id>/<eval_run_id>/evaluation_run.json
  agents/<agent_version_id>/<eval_run_id>/episodes/<episode_result_id>.json
```

Every file is the **unmodified response body** — no pretty-printing, no derived files.

**Race:** agents come from `race.json` → `qualifiers[]`. A run is included only when its
`race_id` matches, `phase == "RACE"`, `is_included == true`, and status is not `STALE`.

**Qualifying:** agents come from `/v1/public/leaderboard` with `final_score >=` the
threshold. A run is included when `phase == "QUALIFYING"`, `is_included == true`, and
status is not `STALE`.

Episodes come from `evaluation_run.json` → `items.items[].episode_result_id`.
`AGENT_CODE`, `EVAL_LOGS_BUNDLE` and `EVAL_PROBLEM_LOGS` are never requested.

Endpoints used: `/v1/public/races/{id}`, `/v1/public/leaderboard`,
`/v1/public/agent-versions/{id}/runs`, `/v1/public/evaluation-runs/{id}`,
`/v1/public/episode-results/{id}/feedback`.

## After a download finishes

With the defaults enabled, main then:

1. Rebuilds `task_dictionary.json` (`UPDATE_TASK_DICTIONARY=1`) via
   `TASK_DICTIONARY_SCRIPT`.
2. Commits and pushes the finished label in ~10% batches under `ORO_LOGS_GIT_DIR`
   (`PUSH_ORO_LOGS=1`). Messages look like `add: Race 22 (i/total)` or
   `add: Qualifying (i/total)`, then `update: task_dictionary.json`.

Do not restart `oro-main` while that background push is still running, or the push can be
cut off (re-run by refreshing the dictionary for that label if needed).

## Resume

Main remembers the running race in `.oro-main-state.json`, so a crash, a `pm2 restart` or
a reboot continues by itself (`AUTO_RESUME=1`, the default) — the startup log says
`resumed race ...`. Stopping a race, from the dashboard button or
`python3 oro_main.py stop`, clears that marker, so a stop stays stopped. Qualifying
resumes the same way when `race_id` is `qualifying`.

Either way the work is picked up from disk: files that already exist with size > 0 are
skipped on enqueue, again when a unit is handed out, and again on the worker itself. The
UI reports them as *already on disk*. Only the handful of episodes in flight at the moment
of the interruption are fetched twice.

Nothing half-written survives an interruption: every file is written to `*.partial` and
renamed, and an upload that stops mid-transfer is rejected rather than saved, so resume
cannot mistake a truncated episode for a finished one.

## Dynamic queue (equal finish times)

The race is **never** pre-split into N equal shares. Main keeps one global queue of small
units (one eval run's remaining episodes, chunked to `UNIT_MAX_EPISODES`), and hands out
exactly **one** unit per request:

- A machine that is also busy with other ORO traffic simply asks for work less often. It
  gets fewer units. Nothing is reserved for it.
- A fast machine comes back immediately and gets more units.
- When the queue is empty and a worker asks for work, main splits the **tail** of the
  largest in-flight unit and gives half of it to the free worker, so the end of a race
  never waits on one overloaded VPS. The robbed worker is told which items it lost on its
  next progress call and drops them.
- If a unit makes no progress for `UNIT_STALE_SEC` (hung or disconnected machine), it goes
  back to the queue.
- A file that keeps failing is dropped after `MAX_ITEM_FAILURES` attempts (default 3) and
  counted as *unavailable*, so one episode that is permanently 404 cannot hold the race
  open for ever.
- Dashboard-**paused** workers stay connected but are never assigned units.
- The race is done when the queue is empty and nothing is in flight.

**Metadata is distributed too.** `runs.json` and `evaluation_run.json` are work units of
their own (`UNIT_MAX_META` files each) and they are always handed out before any episode
unit, so every VPS helps build the file list instead of main fetching thousands of
metadata files alone — on a large race that is the difference between half an hour and a
couple of minutes. Main parses each metadata file as it lands and queues the next level
from it, and the episode phase only begins once no metadata is outstanding.

The per-VPS bars show **recent measured EPM**, not an assigned target, so they move as
machines speed up or slow down.

## Rate limiting and 429s

Each process that talks to ORO has one limiter shared by all its threads: minimum
`60/98`s between requests plus a sliding 60-second window of 98, so there is no burst at
startup. Uploads to main are not rate limited. On HTTP 429 the process parks **all** its
ORO threads for `Retry-After` (default 60) + 15s, clears its window, and reports the 429
to main, where it appears in the UI.

**main's own IP:** `oro-main` (metadata) and main's local `oro-worker` are two processes
sharing one public IP, so `ecosystem.main.cjs` gives each `ORO_MAX_RPM=49`. Once the
metadata phase is finished you can hand the whole budget to the worker:

```bash
ORO_MAX_RPM=90 pm2 restart oro-worker --update-env
```

A worker cannot assume 98/min is free; if the machine has other ORO traffic, lower its
`ORO_MAX_RPM` instead of letting it collect 429s.

## Ports and firewall

One listening port in the whole cluster, on main.

| Process | Listens | Default |
| --- | --- | --- |
| `oro-main` (API + UI) | `BIND_HOST:BIND_PORT` | `0.0.0.0:8080` |
| `oro-worker` (any machine) | **nothing** | — |
| Vite dev server (optional) | `127.0.0.1:5173` | proxies `/api` → 8080 |

Workers are HTTP *clients*, exactly like a browser: they only make **outbound**
connections, to main's `BIND_PORT` and to `api.oroagents.com:443`. Most firewalls allow
outbound already, so no inbound rule is needed on a worker.

```
main:    the port is public when you want the dashboard reachable from anywhere,
         so ALLOWED_IPS / CLUSTER_TOKEN is what protects /api/upload - keep them set.
         To lock everything down instead: allow inbound BIND_PORT only from
         worker IPs, and reach the UI through ssh -L or a VPN.
workers: allow outbound 443 + outbound to main's BIND_PORT; no inbound rule
windows: open BIND_PORT inbound on MAIN only
```

Tailscale/VPN instead of a public port:

```bash
MAIN_URL=http://100.x.y.z:8080     # main's tailscale IP; workers still listen on nothing
```

HTTPS: put a reverse proxy on main (443 → 8080) and set `MAIN_URL=https://main.example.com`.

If you change `BIND_PORT`, update `MAIN_URL` on **every** worker.

## Security

One port serves two route groups, gated independently. A dashboard login grants **no**
upload rights, and a worker token grants **no** dashboard access.

| Routes | Gate |
| --- | --- |
| `GET /api/work`, `PUT /api/upload`, `POST /api/work/*` | `ALLOWED_IPS` and/or `CLUSTER_TOKEN` per `WORKER_AUTH_MODE` |
| dashboard, `/api/status`, `/api/workers`, `POST /api/races`, `POST /api/qualifying`, worker pause/resume | `UI_USER`/`UI_PASSWORD` + `UI_ALLOWED_IPS` if set |
| `GET /api/health` | open, for uptime checks |

**Worker routes — `ALLOWED_IPS` + `WORKER_AUTH_MODE`.** These write into your dataset.
Comma-separated IPs and CIDR blocks; `127.0.0.0/8` and `::1` are always allowed so main's
own worker and CLI keep working. Blocked requests get `403` naming the IP main saw,
logged at most once per IP per minute. Empty `ALLOWED_IPS` means any IP may pass the IP
check — never leave both allowlist and token empty on a public port.

| `WORKER_AUTH_MODE` | Meaning |
| --- | --- |
| `ip_and_token` | Default. Allowlist (if set) **and** token (if set). |
| `ip_or_token` | Allowlist **or** valid token — VPS by IP, home/VPN PC by token ([LocalPC.md](LocalPC.md)). |
| `ip` | Allowlist only. |
| `token` | Token only (any IP with the secret). |

**Dashboard — `UI_USER` / `UI_PASSWORD`.** HTTP Basic, so the browser prompts natively.
After `UI_MAX_FAILS` wrong passwords from one IP, further wrong guesses get `429` for
`UI_BAN_SEC`; the correct password always works, so an attacker cannot lock you out of
your own dashboard. `UI_ALLOWED_IPS` optionally restricts the UI by IP as well.

> **Basic auth is only base64-encoded.** On a public HTTP port the password is
> effectively cleartext to anyone watching the network path. Put main behind the HTTPS
> reverse proxy below, or keep the dashboard on a VPN or SSH tunnel.

`oro-main` prints both policies at startup and warns when either side is `OPEN`.

`X-Forwarded-For` is ignored unless `TRUST_PROXY=1` *and* the connecting peer is
loopback/private, so a client on a public address cannot forge a listed IP. Only enable
`TRUST_PROXY` when your own reverse proxy sits in front of main.

Upload paths are normalized and must consist of plain
`[A-Za-z0-9._-]` segments resolving inside `Data/races/<label>/`; `..`, absolute paths,
drive letters, URL-escaped separators and dotfiles are rejected with 400.

## Development UI

```bash
cd frontend && npm run dev          # 127.0.0.1:5173, proxies /api to 8080
MAIN_URL=http://203.0.113.10:8080 npm run dev   # against a remote main
```

Never expose 5173 in production — `npm run build` and let `oro-main` serve `dist/`.

## Remote machines keep nothing

A worker with `WRITE_LOCAL=0` holds each episode body in memory only and `PUT`s it to
main immediately; it writes no temp file and keeps no copy of the dataset. An item is not
counted as done until main confirms the upload, and uploads retry forever if main is
briefly down. There is no end-of-job tar or rsync, so Windows workers need no extra tools.

## API

| Route | Auth | Purpose |
| --- | --- | --- |
| `GET /api/health` | none | liveness |
| `GET /api/status` | UI login | everything the UI shows (incl. `enabled` / `paused` per worker) |
| `GET /api/workers` | UI login | per-VPS detail |
| `POST /api/races` | UI login | `{race_id, label}` — start/resume a race |
| `POST /api/qualifying` | UI login | `{min_score?, label?}` — qualifying download |
| `POST /api/races/stop` | UI login | stop the race, keep the files, do not auto-resume |
| `POST /api/workers/{id}/pause` | UI login | stop assigning new jobs to that worker |
| `POST /api/workers/{id}/resume` | UI login | allow jobs again |
| `POST /api/workers/pause-all` | UI login | pause every known worker |
| `POST /api/workers/resume-all` | UI login | clear the paused set |
| `GET /api/work?worker_id=` | worker gate | long-poll ~60s → `204` idle, or `200` one unit |
| `PUT /api/upload?label=&relpath=&worker_id=&job_id=` | worker gate | raw body = file bytes; idempotent |
| `POST /api/work/{job_id}/progress` | worker gate | `{done_relpaths, bytes, new_429}` heartbeat |
| `POST /api/work/{job_id}/done` | worker gate | unit finished |
| `POST /api/work/{job_id}/fail` | worker gate | remaining items return to the queue |

CLI extras: `python3 oro_main.py start-qualifying [--min-score 0.55] [--label qualifying]`.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| worker logs `main unreachable` | `MAIN_URL` missing the port, or main's firewall blocks the worker IP |
| worker logs `401` | `CLUSTER_TOKEN` differs from main's (or local PC missing the token under `ip_or_token`) |
| worker logs `403` | that worker's public IP is not in main's `ALLOWED_IPS` (or use `ip_or_token` + token for VPN PCs) |
| Windows: `SSL: CERTIFICATE_VERIFY_FAILED` | `python -m pip install --upgrade certifi`, restart the worker |
| UI says `cannot reach oro-main` | `oro-main` is down (`pm2 logs oro-main`) |
| browser keeps asking for the password | `UI_USER`/`UI_PASSWORD` typo in `.env`; check `pm2 logs oro-main` for the active policy |
| `429 too many failed logins` | wrong password tried too often from your IP; the correct one still works immediately |
| `403 ... is not in ALLOWED_IPS` | a **worker's** IP is missing from `ALLOWED_IPS`; add it and `pm2 start ecosystem.main.cjs` |
| `403 ... is not in UI_ALLOWED_IPS` | you restricted the dashboard by IP; add yours, or tunnel in: `ssh -L 8080:127.0.0.1:8080 main` |
| plain page saying the build is missing | run `npm run build` in `frontend/` |
| many 429s on one machine | that IP has other ORO traffic; lower its `ORO_MAX_RPM` |
| extra VPS does not speed things up | it shares a public IP with another worker |
| `409 race already running` | wait for the current race, or it is still fetching metadata |
| worker stuck `paused` | resume it from the dashboard, or `POST /api/workers/{id}/resume` |
| race finished but no git push | main was restarted mid-push; re-run the dictionary/push for that label |
