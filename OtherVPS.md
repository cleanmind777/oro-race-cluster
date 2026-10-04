# OtherVPS.md — set up an EXTRA worker machine (Linux or Windows)

An extra machine runs one process, `oro-worker`. It:

- **listens on no port at all** — it is an HTTP client, like a browser,
- long-polls main for **one small unit** of work at a time,
- downloads each episode from `api.oroagents.com`,
- uploads each file to main **immediately** after downloading it,
- keeps **no copy** of the race data (bytes are held in memory, never written to disk),
- waits forever under PM2, so a new race needs no command on this machine.

Set up main first with **MainVPS.md**.

> **An extra machine only helps if it has a DIFFERENT public IP from the others.**
> The ORO cap is per public IP, so two workers behind one IP are no faster than one.
> Check with `curl -s https://api.ipify.org` on each machine; the values must differ.

---

## 1. What you need from main

| Value | Example | Where it comes from |
| --- | --- | --- |
| `MAIN_URL` | `http://203.0.113.10:8080` | main's IP or hostname **plus the port** |
| `WORKER_ID` | `vps-fra-1` | your choice, unique per machine |
| `CLUSTER_TOKEN` | *(usually empty)* | only if main was configured with one |

A worker never needs main's dashboard login: `UI_USER` / `UI_PASSWORD` are not worker
settings and do not belong in this machine's `.env`.

`MAIN_URL` must include the port unless main sits behind an HTTPS proxy on 443
(`MAIN_URL=https://main.example.com`). A Tailscale/VPN address works too
(`http://100.x.y.z:8080`) and needs no public port anywhere.

### Register this machine's public IP on main first

Main only accepts **worker** requests from the addresses in its `ALLOWED_IPS`. (The
dashboard is gated by a username and password instead, which workers never use.) Get this
machine's public IP:

```bash
curl -s https://api.ipify.org
```

Add it to `ALLOWED_IPS` in main's `.env`, then on **main**:

```bash
pm2 restart oro-main
```

Then check reachability from here before installing anything:

```bash
curl -s http://203.0.113.10:8080/api/health
# {"ok": true, "phase": "idle", ...}          <- good
# 403 - 198.51.100.30 is not in ALLOWED_IPS   <- add that IP on main (above)
# (timeout)                                   <- firewall / security group, see section 5
```

The 403 body tells you exactly which address main saw, which is the one to list — handy
when the machine sits behind NAT.

---

## 2. Files to copy

Only four files are needed:

```
oro_worker.py
ecosystem.worker.cjs
pm2_start.cjs            # required by ecosystem.worker.cjs
.env.example             # copy to .env and edit (section 3.2 / 4.2)
```

Copying the whole repo is also fine. The worker needs no `frontend/` build and no
`Data/` directory. Do **not** copy main's `.env` as-is — this machine needs a different
one (`MAIN_URL` and `WORKER_ID`, no `ALLOWED_IPS` or `BIND_*`).

---

## 3. Linux

### 3.1 Install

```bash
python3 --version          # 3.9+ ; no pip packages needed
node --version             # 18+ , only because PM2 is a Node tool
npm install -g pm2
```

If Python is missing: `sudo apt install -y python3` (Debian/Ubuntu) or
`sudo dnf install -y python3` (RHEL family). If Node is missing, install it from
NodeSource or your distro, then `npm install -g pm2`.

### 3.2 Configure `.env`

All settings live in `.env` next to `oro_worker.py`. Nothing needs exporting.

```bash
cd ~/oro-race-cluster
cp .env.example .env
nano .env
```

A worker only needs these lines — delete or ignore the main-side ones:

```ini
MAIN_URL=http://203.0.113.10:8080     # main's address, WITH the port
WORKER_ID=vps-fra-1                   # unique per machine
WORKER_THREADS=4
ORO_MAX_RPM=98                        # lower it if this VPS already calls ORO
CLUSTER_TOKEN=                        # only if main has one; must match exactly
```

### 3.3 Start

```bash
pm2 start ecosystem.worker.cjs
pm2 save
pm2 startup        # prints a command; run that command so it survives reboots
```

### 3.4 Verify

```bash
pm2 status                                   # oro-worker online, restarts 0
pm2 logs oro-worker --lines 5 --nostream
```

Expected first line:

```
[vps-fra-1 12:00:00] waiting for work from http://203.0.113.10:8080  write_local=0  threads=4  limit=98/min
```

`write_local=0` is correct for an extra machine: it uploads instead of writing locally.

Then confirm main sees it — run this anywhere, no token needed:

```bash
curl -s http://203.0.113.10:8080/api/workers
```

Your `worker_id` should be listed with status `waiting`. It now stays idle until someone
starts a race **on main**. You never run a command here again.

### 3.5 Confirm it listens on nothing

```bash
ss -ltnp | grep python     # expect no output
```

---

## 4. Windows

The same `oro_worker.py` runs on Windows; it uses `pathlib`, needs no bash, and never
needs rsync or tar.

### 4.1 Install

1. **Python 3** from <https://www.python.org/downloads/> — tick
   *"Add python.exe to PATH"* during setup.
   Avoid the Microsoft Store build; its `python3` alias confuses process managers.
2. **Node LTS** from <https://nodejs.org/>.
3. Open **PowerShell** and install PM2:

```powershell
python --version
node --version
npm install -g pm2
```

### 4.2 Configure `.env`

Same single file as on Linux, plus one Windows-specific line: PM2 must be told the
interpreter is `python`, not `python3`.

```powershell
cd C:\oro-race-cluster
Copy-Item .env.example .env
notepad .env
```

```ini
MAIN_URL=http://203.0.113.10:8080
WORKER_ID=win-nyc-1
WORKER_THREADS=4
ORO_MAX_RPM=98
CLUSTER_TOKEN=
PYTHON=python
```

If `python` is not on PATH, give the full path instead (forward slashes or doubled
backslashes both work):

```ini
PYTHON=C:/Users/me/AppData/Local/Programs/Python/Python312/python.exe
```

### 4.3 Start

```powershell
pm2 start ecosystem.worker.cjs
pm2 save
```

### 4.4 Verify

```powershell
pm2 status
pm2 logs oro-worker --lines 5 --nostream
```

Expect the same `waiting for work ... write_local=0` line as on Linux. Then check from
any machine that main lists this `worker_id`:

```powershell
curl.exe http://203.0.113.10:8080/api/workers
```

Confirm it listens on nothing — no `python.exe` should appear as `LISTENING`:

```powershell
netstat -ano | Select-String LISTENING | Select-String (Get-Process python).Id
# expect no output
```

### 4.5 Start on boot (PM2's `pm2 startup` does not work on Windows)

`pm2 startup` fails on Windows with *"Init system not found"*. Use Task Scheduler to run
`pm2 resurrect`, which restores the saved app list; the worker re-reads `.env` itself on
every start.

Make sure you ran `pm2 save` first, then in an **Administrator** PowerShell:

```powershell
$pm2 = (Get-Command pm2.cmd).Source
$action  = New-ScheduledTaskAction -Execute $pm2 -Argument "resurrect"
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
  -LogonType S4U -RunLevel Highest
Register-ScheduledTask -TaskName "pm2-resurrect" -Action $action -Trigger $trigger `
  -Principal $principal -Description "Restore PM2 apps (oro-worker) at boot"
```

Use the **same user account** that ran `pm2 save`, because the saved app list lives in
that user's profile (`%USERPROFILE%\.pm2\dump.pm2`).

Test it without rebooting:

```powershell
pm2 kill
Start-ScheduledTask -TaskName "pm2-resurrect"
Start-Sleep -Seconds 5
pm2 status            # oro-worker online again
```

Alternative: set the trigger to *At log on* if the machine always has a logged-in session,
or run `pm2 resurrect` from a shortcut in
`shell:startup`.

---

## 5. Firewall

**This machine needs no inbound rule.** "Opening a port" means accepting inbound
connections, and the worker never does that. It only makes outbound connections:

| Destination | Port | Why |
| --- | --- | --- |
| main VPS | `BIND_PORT` (default 8080) | pull work, upload files, report progress |
| `api.oroagents.com` | 443 | download race logs |

Outbound traffic is allowed by default on stock Linux, Windows and most VPS providers, so
usually there is nothing to do.

If your provider blocks outbound by default:

```bash
# ufw, if this machine has it; many VPS images ship with no firewall at all
# (command -v ufw prints nothing), and then there is nothing to allow
sudo ufw allow out 443/tcp
sudo ufw allow out to 203.0.113.10 port 8080 proto tcp
```

```powershell
# Windows, only if outbound is blocked by policy
New-NetFirewallRule -DisplayName "oro-worker out" -Direction Outbound -Protocol TCP `
  -RemotePort 443,8080 -Action Allow
```

The inbound rule for `BIND_PORT` belongs on **main only**. This machine needs no
inbound rule at all: the worker never listens on a port.

---

## 6. Variables

All of these go in `.env`. Apply edits with `pm2 start ecosystem.worker.cjs`, which
re-reads the file; a plain `pm2 restart` keeps the values the app was started with. A
real environment variable wins for a one-off change:
`ORO_MAX_RPM=40 pm2 restart oro-worker --update-env`.

| Key | Default | Meaning |
| --- | --- | --- |
| `MAIN_URL` | `http://127.0.0.1:8080` | **required**; main's address, must include the port |
| `WORKER_ID` | machine hostname | unique label in the dashboard |
| `CLUSTER_TOKEN` | *(empty = not sent)* | only if main has one; must match exactly |
| `WORKER_THREADS` | `4` | parallel ORO downloads, all under one limiter |
| `ORO_MAX_RPM` | `98` | per-process cap toward ORO |
| `PYTHON` | `python3` | set to `python` on Windows |
| `WRITE_LOCAL` | set to `0` by PM2 | never change it; `1` is only for main's own worker |

### When to lower `ORO_MAX_RPM`

If this machine **already calls the ORO API for something else** (a validator, a miner,
another script), it does not have the full ~100 requests/min to itself. Give the worker
only what is spare:

```bash
ORO_MAX_RPM=40 pm2 restart oro-worker --update-env
```

This costs you nothing in fairness: main hands out work on demand, so a slower machine
simply asks less often and receives fewer units. Nothing is reserved for it and no fixed
share is assigned, so the race is not held back by the slowest machine.

If you do not lower it, nothing breaks either — the worker will collect HTTP 429s, park
all its ORO threads for `Retry-After` + 15 s, report the 429 to main (visible in the
dashboard as `rate-limited`), and carry on by itself.

---

## 7. What this machine does and does not keep

Each episode body is held in memory, uploaded to main, and dropped. No temp files, no
partial copies, no end-of-job tar or rsync. An item is not counted as done until main
confirms the upload, and if main is briefly down the upload retries with backoff until it
succeeds, so nothing is silently lost.

If main is restarted or unreachable, the worker logs `main unreachable` and keeps retrying.
It rejoins by itself; no action needed here.

---

## 8. Day-to-day

```bash
pm2 status                      # is the worker alive
pm2 logs oro-worker             # live view of downloads, 429s, uploads
pm2 logs oro-worker --lines 50 --nostream
pm2 restart oro-worker          # safe at any time; in-flight work is requeued by main
pm2 stop oro-worker             # take this machine out of the cluster
pm2 start oro-worker            # put it back; it will get work within seconds
pm2 flush                       # truncate logs
```

To change main's address, the rate cap or the token, edit `.env` and start from the
config file again so the new values are read:

```bash
nano .env
pm2 start ecosystem.worker.cjs
```

After copying a new `oro_worker.py`: `pm2 restart oro-worker`.

---

## 9. Adding more machines

Repeat this file on each one. Only two things matter:

1. a **different public IP** from every other worker,
2. a **unique** `WORKER_ID`.

No change is needed on main — new workers simply appear in the dashboard, and the queue
redistributes itself. You can add a machine while a race is running; it will start pulling
units immediately, including stolen tail work from slower machines.

---

## 10. Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `main unreachable` | `MAIN_URL` missing the port, wrong IP, or main's firewall / cloud security group blocks this IP. Test with `curl http://MAIN:8080/api/health` |
| `403 ... is not in ALLOWED_IPS` | this machine's public IP is not listed on main; add it to main's `.env` and run `pm2 start ecosystem.main.cjs` there |
| an edited `.env` seems ignored | you used `pm2 restart`; run `pm2 start ecosystem.worker.cjs` instead |
| worked before, now 403 | this machine's public IP changed; update main's `ALLOWED_IPS` |
| `CLUSTER_TOKEN does not match main` (401) | token typo, or main has one and this `.env` leaves it empty; fix `.env` and `pm2 restart oro-worker` |
| worker not in the dashboard | it is not running (`pm2 status`), or it cannot reach main |
| row shows `offline` | process died or lost its network; `pm2 logs oro-worker`. Main requeues its work automatically after 180 s |
| row shows `rate-limited` often | this IP is near the ORO cap; lower `ORO_MAX_RPM` |
| it never gets work | normal if no race is running, or the race is finished; start a race on main |
| files done stays 0 while others climb | check for 429s in `pm2 logs oro-worker`, then lower `ORO_MAX_RPM` |
| `pm2 start` says `Script already launched` | a stray `ecosystem.worker` entry from an interrupted start; `pm2 delete ecosystem.worker` then start again |
| `pm2 list` shows an `ecosystem.worker` app | same leftover wrapper; `pm2 delete ecosystem.worker`. Only `oro-worker` should be listed |
| Windows: `interpreter python3 not found` | set `$env:PYTHON = "python"` (or the full `python.exe` path) and start again |
| Windows: nothing runs after reboot | `pm2 save` was not run, or the Task Scheduler task runs as a different user than the one that saved |
| adding the machine did not speed things up | it shares a public IP with another worker |
