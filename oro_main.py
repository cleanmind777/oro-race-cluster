#!/usr/bin/env python3
"""oro-main: coordinator for downloading raw ORO public race logs.

Runs on the MAIN VPS only. Serves the worker API, the React build, and holds the
single durable copy of the data under DATA_DIR/<label>/.

CLI:
    python oro_main.py serve
    python oro_main.py start --race-id UUID [--label race2]
    python oro_main.py status
"""

from __future__ import annotations

import argparse
import base64
import collections
import hmac
import ipaddress
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_env_file(path: Path) -> None:
    """Read KEY=VALUE lines from .env. The real environment always wins, so a
    one-off `VAR=x pm2 restart ... --update-env` still overrides the file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if value[:1] in ("\"", "'"):
            quote = value[0]  # quoted: literal, spaces and # included
            end = value.find(quote, 1)
            value = value[1:end] if end != -1 else value[1:]
        else:
            value = re.split(r"\s#", " " + value, maxsplit=1)[0].strip()  # trailing comment
        if key and not os.environ.get(key):
            os.environ[key] = value


load_env_file(ROOT / ".env")

API_BASE = os.environ.get("ORO_API_BASE", "https://api.oroagents.com").rstrip("/")
BIND_HOST = os.environ.get("BIND_HOST", "0.0.0.0")
BIND_PORT = int(os.environ.get("BIND_PORT", "8080"))
CLUSTER_TOKEN = os.environ.get("CLUSTER_TOKEN", "")
ALLOWED_IPS = os.environ.get("ALLOWED_IPS", "")
TRUST_PROXY = os.environ.get("TRUST_PROXY", "0").strip() in ("1", "true", "yes")
# dashboard login; the worker routes never use it
UI_USER = os.environ.get("UI_USER", "")
UI_PASSWORD = os.environ.get("UI_PASSWORD", "")
UI_ALLOWED_IPS = os.environ.get("UI_ALLOWED_IPS", "")
UI_MAX_FAILS = int(os.environ.get("UI_MAX_FAILS", "10"))
UI_BAN_SEC = float(os.environ.get("UI_BAN_SEC", "300"))
DATA_DIR = Path(os.environ.get("DATA_DIR") or (ROOT / "Data" / "races")).resolve()
DIST_DIR = Path(os.environ.get("DIST_DIR") or (ROOT / "frontend" / "dist")).resolve()

# main's metadata fetch shares its public IP with main's local worker, so it
# takes half of the ~98/min budget by default
MAX_RPM = int(os.environ.get("MAIN_ORO_MAX_RPM") or os.environ.get("ORO_MAX_RPM") or "49")
UNIT_MAX_EPISODES = int(os.environ.get("UNIT_MAX_EPISODES", "20"))
LONGPOLL_SEC = float(os.environ.get("LONGPOLL_SEC", "60"))
UNIT_STALE_SEC = float(os.environ.get("UNIT_STALE_SEC", "180"))
STEAL_MIN_ITEMS = int(os.environ.get("STEAL_MIN_ITEMS", "4"))
WORKER_OFFLINE_SEC = float(os.environ.get("WORKER_OFFLINE_SEC", "90"))
RATE_WINDOW_SEC = 300.0
USER_AGENT = "oro-race-cluster/1.0"


# --------------------------------------------------------------------------- #
# rate limiter: one per process, ORO traffic only
# --------------------------------------------------------------------------- #
class RateLimiter:
    """Min-interval + sliding 60s window, with a 429 penalty box."""

    def __init__(self, max_per_min: int = MAX_RPM):
        self.max_per_min = max_per_min
        self.min_interval = 60.0 / max_per_min
        self._window: collections.deque[float] = collections.deque()
        self._last = 0.0
        self._blocked_until = 0.0
        self._cond = threading.Condition(threading.Lock())

    def acquire(self) -> None:
        with self._cond:
            while True:
                now = time.monotonic()
                waits = []
                if now < self._blocked_until:
                    waits.append(self._blocked_until - now)
                if self._last and now - self._last < self.min_interval:
                    waits.append(self.min_interval - (now - self._last))
                while self._window and now - self._window[0] >= 60.0:
                    self._window.popleft()
                if len(self._window) >= self.max_per_min:
                    waits.append(60.0 - (now - self._window[0]))
                if not waits:
                    self._last = now
                    self._window.append(now)
                    return
                self._cond.wait(max(min(waits), 0.01))

    def penalize(self, retry_after: float) -> float:
        """Park every ORO thread in this process, then start the window fresh."""
        with self._cond:
            until = time.monotonic() + retry_after + 15.0
            self._blocked_until = max(self._blocked_until, until)
            self._window.clear()
            self._cond.notify_all()
            return self._blocked_until - time.monotonic()


def parse_retry_after(value: str | None) -> float:
    try:
        return max(float(value), 1.0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 60.0


def oro_get(url: str, limiter: RateLimiter, on_429=None, attempts: int = 6,
            timeout: float = 120.0) -> bytes:
    """GET raw bytes from the ORO public API through the limiter."""
    if not url.startswith("http"):
        url = API_BASE + url
    last_err: Exception | None = None
    for attempt in range(attempts):
        limiter.acquire()
        req = urllib.request.Request(
            url, headers={"Accept": "application/json", "User-Agent": USER_AGENT}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as err:
            err.read()
            if err.code == 429:
                wait = limiter.penalize(parse_retry_after(err.headers.get("Retry-After")))
                if on_429:
                    on_429()
                log(f"429 from ORO, pausing {wait:.0f}s")
                last_err = err
                continue
            if err.code in (500, 502, 503, 504):
                last_err = err
                time.sleep(min(2 ** attempt, 15))
                continue
            raise
        except (urllib.error.URLError, OSError, TimeoutError) as err:
            last_err = err
            time.sleep(min(2 ** attempt, 15))
    raise last_err if last_err else RuntimeError(f"GET failed: {url}")


def log(msg: str) -> None:
    print(f"[main {time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# IP allowlist: the whole server, not just the worker routes
# --------------------------------------------------------------------------- #
def parse_allowlist(raw: str) -> list:
    """ALLOWED_IPS is a comma/space separated list of IPs and CIDR blocks."""
    nets = []
    for part in re.split(r"[,\s]+", raw or ""):
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            log(f"ignoring unparseable ALLOWED_IPS entry: {part!r}")
    return nets


ALLOW_NETS = parse_allowlist(ALLOWED_IPS)      # worker routes
UI_ALLOW_NETS = parse_allowlist(UI_ALLOWED_IPS)  # dashboard, empty = any IP
# main's own worker and the CLI connect over loopback, so it is always allowed
LOOPBACK_NETS = [ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128")]

_rejected_at: dict[str, float] = {}
_rejected_lock = threading.Lock()


def normalize_ip(raw: str):
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return None
    return ip.ipv4_mapped or ip if isinstance(ip, ipaddress.IPv6Address) else ip


def client_ip(peer: str, forwarded_for: str | None):
    ip = normalize_ip(peer)
    if ip is None:
        return None
    if TRUST_PROXY and (ip.is_loopback or ip.is_private) and forwarded_for:
        # the rightmost entry is the one our own reverse proxy appended, so it
        # cannot be forged by the client (nginx: $proxy_add_x_forwarded_for)
        candidate = normalize_ip(forwarded_for.split(",")[-1].strip())
        if candidate is not None:
            ip = candidate
    return ip


def ip_allowed(peer: str, forwarded_for: str | None, nets: list) -> tuple[bool, str]:
    """Returns (allowed, ip_we_judged). An empty net list means allow everyone."""
    ip = client_ip(peer, forwarded_for)
    if ip is None:
        return False, peer
    if not nets:
        return True, str(ip)
    for net in LOOPBACK_NETS + nets:
        if ip.version == net.version and ip in net:
            return True, str(ip)
    return False, str(ip)


def note_rejected(ip: str, path: str, why: str) -> None:
    """Log a blocked IP at most once a minute so a scanner cannot flood the log."""
    now = time.monotonic()
    key = f"{ip}|{why}"
    with _rejected_lock:
        if now - _rejected_at.get(key, 0.0) < 60.0:
            return
        _rejected_at[key] = now
    log(f"blocked {ip} on {path} ({why})")


# --------------------------------------------------------------------------- #
# dashboard login: HTTP Basic, with a crude brute-force brake
# --------------------------------------------------------------------------- #
_fails: dict[str, list] = {}  # ip -> [count, window_start]
_fails_lock = threading.Lock()


def login_banned(ip: str) -> bool:
    with _fails_lock:
        entry = _fails.get(ip)
        if not entry:
            return False
        count, started = entry
        if time.monotonic() - started > UI_BAN_SEC:
            del _fails[ip]
            return False
        return count >= UI_MAX_FAILS


def note_login_failure(ip: str) -> None:
    with _fails_lock:
        entry = _fails.get(ip)
        now = time.monotonic()
        if not entry or now - entry[1] > UI_BAN_SEC:
            _fails[ip] = [1, now]
            return
        entry[0] += 1
        if entry[0] == UI_MAX_FAILS:
            log(f"too many failed UI logins from {ip}; blocked for {UI_BAN_SEC:.0f}s")


def note_login_success(ip: str) -> None:
    with _fails_lock:
        _fails.pop(ip, None)


def basic_auth_ok(header: str | None) -> bool:
    """Constant-time check of the Authorization header against UI_USER/UI_PASSWORD."""
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        raw = base64.b64decode(header.split(None, 1)[1], validate=True)
        user, _, password = raw.decode("utf-8", "replace").partition(":")
    except (ValueError, IndexError):
        return False
    return (hmac.compare_digest(user, UI_USER)
            and hmac.compare_digest(password, UI_PASSWORD))


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #
class UnsafePath(ValueError):
    pass


# Every path segment we ever write is a UUID or a fixed name, so a strict
# whitelist is enough: no leading dot, no separators, no escapes, no unicode.
SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def race_dir(label: str) -> Path:
    if not label or not SAFE_SEGMENT.match(label):
        raise UnsafePath(f"bad label: {label!r}")
    return DATA_DIR / label


def safe_dest(label: str, relpath: str) -> Path:
    """Resolve relpath strictly inside DATA_DIR/<label>/."""
    base = race_dir(label)
    rel = (relpath or "").replace("\\", "/")
    if rel.startswith("/") or ":" in rel or "\0" in rel:
        raise UnsafePath(f"bad relpath: {relpath!r}")
    parts = []
    for part in rel.split("/"):
        if part in ("", "."):
            continue
        if not SAFE_SEGMENT.match(part):
            raise UnsafePath(f"bad relpath segment {part!r} in {relpath!r}")
        parts.append(part)
    if not parts:
        raise UnsafePath("empty relpath")
    base.mkdir(parents=True, exist_ok=True)
    dest = base.joinpath(*parts)
    try:
        dest.resolve().relative_to(base.resolve())
    except ValueError:
        raise UnsafePath(f"escapes race dir: {relpath!r}") from None
    return dest


def write_atomic(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".partial")
    tmp.write_bytes(data)
    os.replace(tmp, dest)


def exists_nonempty(path: Path) -> bool:
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# cluster state
# --------------------------------------------------------------------------- #
class Worker:
    def __init__(self, worker_id: str):
        self.worker_id = worker_id
        self.first_seen = time.time()
        self.last_seen = time.time()
        self.files_done = 0
        self.units_done = 0
        self.bytes = 0
        self.oro_429 = 0
        self.job_id: str | None = None
        self.in_flight = 0
        self.paused_until = 0.0  # worker is parked on an ORO 429
        self.completions: collections.deque[float] = collections.deque()

    def note_file(self, when: float) -> None:
        self.completions.append(when)
        self.trim(when)

    def trim(self, now: float) -> None:
        while self.completions and now - self.completions[0] > RATE_WINDOW_SEC:
            self.completions.popleft()

    def epm(self, now: float) -> float:
        self.trim(now)
        if not self.completions:
            return 0.0
        span = max(now - self.completions[0], 30.0)
        return len(self.completions) * 60.0 / span

    def status(self, now: float) -> str:
        if now - self.last_seen > WORKER_OFFLINE_SEC:
            return "offline"
        if now < self.paused_until:
            return "rate-limited"
        return "downloading" if self.job_id else "waiting"


class State:
    def __init__(self):
        self.cond = threading.Condition(threading.RLock())
        self.race_id: str | None = None
        self.label: str | None = None
        self.phase = "idle"  # idle | metadata | episodes | complete | error
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.meta = {"agents_total": 0, "agents_done": 0, "runs_included": 0,
                     "runs_saved": 0, "episodes_found": 0}
        self.pending: collections.deque[list[dict]] = collections.deque()
        self.jobs: dict[str, dict] = {}
        self.workers: dict[str, Worker] = {}
        self.done_set: set[str] = set()
        self.preexisting = 0
        self.total_items = 0
        self.completions: collections.deque[float] = collections.deque()
        self.events: collections.deque[dict] = collections.deque(maxlen=50)
        self.oro_429_total = 0
        self.metadata_429 = 0
        self.requeues = 0
        self.steals = 0
        self.meta_thread: threading.Thread | None = None
        self.limiter = RateLimiter()

    # -- helpers (call with lock held) ------------------------------------- #
    def note_event(self, kind: str, msg: str) -> None:
        self.events.append({"ts": time.time(), "kind": kind, "msg": msg})
        log(f"{kind}: {msg}")

    def in_flight_count(self) -> int:
        return sum(1 for j in self.jobs.values() for i in j["items"]
                   if i["relpath"] not in self.done_set)

    def pending_count(self) -> int:
        return sum(len(u) for u in self.pending)

    def mark_done(self, relpath: str, worker: Worker | None, nbytes: int, now: float,
                  count_rate: bool = True) -> bool:
        if relpath in self.done_set:
            return False
        self.done_set.add(relpath)
        if count_rate:  # files found already on disk must not inflate the rate
            self.completions.append(now)
        while self.completions and now - self.completions[0] > RATE_WINDOW_SEC:
            self.completions.popleft()
        if worker is not None:
            worker.files_done += 1
            worker.bytes += nbytes
            worker.note_file(now)
        return True

    def fpm(self, now: float) -> float:
        while self.completions and now - self.completions[0] > RATE_WINDOW_SEC:
            self.completions.popleft()
        if not self.completions:
            return 0.0
        span = max(now - self.completions[0], 30.0)
        return len(self.completions) * 60.0 / span

    def is_complete(self) -> bool:
        return (self.phase == "episodes" and not self.pending and not self.jobs)

    def enqueue_episodes(self, label: str, relpath_urls: list[tuple[str, str]]) -> None:
        """Add one eval run's episodes, chunked, skipping files already on disk."""
        fresh = []
        for relpath, url in relpath_urls:
            self.total_items += 1
            if relpath in self.done_set:
                continue
            if exists_nonempty(race_dir(label) / relpath):
                self.done_set.add(relpath)
                self.preexisting += 1
                continue
            fresh.append({"relpath": relpath, "url": url})
        for i in range(0, len(fresh), UNIT_MAX_EPISODES):
            self.pending.append(fresh[i:i + UNIT_MAX_EPISODES])
        if fresh:
            self.cond.notify_all()


STATE = State()


# --------------------------------------------------------------------------- #
# metadata fetch: race -> qualifiers -> runs -> eval runs -> episode queue
# --------------------------------------------------------------------------- #
def metadata_worker(race_id: str, label: str) -> None:
    limiter = STATE.limiter

    def on_429() -> None:
        with STATE.cond:
            STATE.oro_429_total += 1
            STATE.metadata_429 += 1

    def fetch(url: str) -> bytes:
        return oro_get(url, limiter, on_429=on_429)

    try:
        base = race_dir(label)
        base.mkdir(parents=True, exist_ok=True)
        raw = fetch(f"/v1/public/races/{race_id}")
        write_atomic(base / "race.json", raw)
        race = json.loads(raw)
        qualifiers = race.get("qualifiers") or []
        agent_ids = []
        for q in qualifiers:
            av = q.get("agent_version_id")
            if av and av not in agent_ids:
                agent_ids.append(av)
        with STATE.cond:
            STATE.meta["agents_total"] = len(agent_ids)
            STATE.note_event("metadata", f"race {race.get('race', {}).get('race_number')} "
                                         f"has {len(agent_ids)} qualifiers")

        for agent_id in agent_ids:
            try:
                raw_runs = fetch(f"/v1/public/agent-versions/{agent_id}/runs")
            except Exception as err:  # noqa: BLE001 - keep going past one bad agent
                with STATE.cond:
                    STATE.note_event("warn", f"runs failed for {agent_id}: {err}")
                    STATE.meta["agents_done"] += 1
                continue
            write_atomic(base / "agents" / agent_id / "runs.json", raw_runs)
            try:
                runs = json.loads(raw_runs)
            except json.JSONDecodeError:
                runs = []
            included = [
                r for r in runs
                if str(r.get("race_id")) == race_id
                and r.get("phase") == "RACE"
                and r.get("is_included") is True
                and r.get("status") != "STALE"
            ]
            with STATE.cond:
                STATE.meta["runs_included"] += len(included)

            for run in included:
                eval_run_id = run.get("eval_run_id")
                if not eval_run_id:
                    continue
                run_dir = base / "agents" / agent_id / eval_run_id
                detail_path = run_dir / "evaluation_run.json"
                try:
                    if exists_nonempty(detail_path):
                        raw_detail = detail_path.read_bytes()
                    else:
                        raw_detail = fetch(f"/v1/public/evaluation-runs/{eval_run_id}")
                        write_atomic(detail_path, raw_detail)
                    detail = json.loads(raw_detail)
                except Exception as err:  # noqa: BLE001
                    with STATE.cond:
                        STATE.note_event("warn", f"eval run {eval_run_id} failed: {err}")
                    continue

                items = (detail.get("items") or {}).get("items") or []
                episodes = []
                for item in items:
                    ep = item.get("episode_result_id")
                    if not ep:
                        continue
                    episodes.append((
                        f"agents/{agent_id}/{eval_run_id}/episodes/{ep}.json",
                        f"{API_BASE}/v1/public/episode-results/{ep}/feedback",
                    ))
                with STATE.cond:
                    STATE.meta["runs_saved"] += 1
                    STATE.meta["episodes_found"] += len(episodes)
                    STATE.enqueue_episodes(label, episodes)

            with STATE.cond:
                STATE.meta["agents_done"] += 1

        with STATE.cond:
            STATE.phase = "episodes"
            STATE.note_event("metadata", f"metadata done: {STATE.meta['runs_saved']} runs, "
                                         f"{STATE.total_items} episodes "
                                         f"({STATE.preexisting} already on disk)")
            STATE.cond.notify_all()
    except Exception as err:  # noqa: BLE001
        with STATE.cond:
            STATE.phase = "error"
            STATE.note_event("error", f"metadata fetch failed: {err}")
            STATE.cond.notify_all()


def start_race(race_id: str, label: str | None) -> dict:
    race_id = str(race_id).strip()
    try:
        uuid.UUID(race_id)
    except ValueError:
        raise UnsafePath("race_id must be a UUID") from None

    with STATE.cond:
        busy = STATE.phase in ("metadata", "episodes") and not STATE.is_complete()
        if busy:
            raise RuntimeError(f"race {STATE.race_id} is still running ({STATE.phase})")

    if not label:
        label = resolve_label(race_id)
    race_dir(label)  # validates

    with STATE.cond:
        STATE.race_id = race_id
        STATE.label = label
        STATE.phase = "metadata"
        STATE.started_at = time.time()
        STATE.finished_at = None
        STATE.meta = {"agents_total": 0, "agents_done": 0, "runs_included": 0,
                      "runs_saved": 0, "episodes_found": 0}
        STATE.pending.clear()
        STATE.jobs.clear()
        STATE.done_set.clear()
        STATE.completions.clear()
        STATE.preexisting = 0
        STATE.total_items = 0
        STATE.requeues = 0
        STATE.steals = 0
        STATE.metadata_429 = 0
        for worker in STATE.workers.values():
            worker.job_id = None
            worker.in_flight = 0
            worker.files_done = 0
            worker.units_done = 0
            worker.bytes = 0
            worker.oro_429 = 0
            worker.completions.clear()
        STATE.note_event("race", f"start {race_id} -> {label}")
        thread = threading.Thread(target=metadata_worker, args=(race_id, label),
                                  daemon=True, name="metadata")
        STATE.meta_thread = thread
        thread.start()
        return {"race_id": race_id, "label": label, "phase": STATE.phase}


def resolve_label(race_id: str) -> str:
    """Default label is race<race_number> straight from race.json."""
    raw = oro_get(f"/v1/public/races/{race_id}", STATE.limiter)
    number = (json.loads(raw).get("race") or {}).get("race_number")
    return f"race{number}" if number is not None else f"race-{race_id[:8]}"


# --------------------------------------------------------------------------- #
# scheduling
# --------------------------------------------------------------------------- #
def steal_tail(worker: Worker) -> None:
    """Split the biggest in-flight unit back into the queue.

    Only used when the queue is empty: otherwise the end of a race waits on
    whichever VPS is slowest, while fast workers sit idle. The robbed worker
    learns which items it lost from its next progress reply.
    Caller holds STATE.cond.
    """
    victim, remaining = None, []
    for job in STATE.jobs.values():
        if job["worker_id"] == worker.worker_id:
            continue
        rem = [i for i in job["items"] if i["relpath"] not in STATE.done_set]
        if len(rem) > len(remaining):
            victim, remaining = job, rem
    if victim is None or len(remaining) < STEAL_MIN_ITEMS:
        return
    keep = len(remaining) - len(remaining) // 2
    stolen = remaining[keep:]
    stolen_paths = {i["relpath"] for i in stolen}
    victim["items"] = [i for i in victim["items"] if i["relpath"] not in stolen_paths]
    victim.setdefault("dropped", []).extend(sorted(stolen_paths))
    STATE.pending.append(stolen)
    STATE.steals += 1
    STATE.note_event("steal", f"{worker.worker_id} took {len(stolen)} tail episodes "
                              f"from {victim['worker_id']}")


def take_unit(worker: Worker) -> dict | None:
    """Hand out ONE small unit. Caller holds STATE.cond."""
    label = STATE.label
    if not label:
        return None
    if not STATE.pending:
        steal_tail(worker)
    while STATE.pending:
        unit = STATE.pending.popleft()
        items = []
        for item in unit:
            if item["relpath"] in STATE.done_set:
                continue
            if exists_nonempty(race_dir(label) / item["relpath"]):
                STATE.mark_done(item["relpath"], None, 0, time.time(), count_rate=False)
                STATE.preexisting += 1
                continue
            items.append(item)
        if not items:
            continue
        job_id = uuid.uuid4().hex[:12]
        now = time.time()
        STATE.jobs[job_id] = {
            "job_id": job_id, "worker_id": worker.worker_id, "items": items,
            "started": now, "last_progress": now, "label": label,
            "race_id": STATE.race_id,
        }
        worker.job_id = job_id
        worker.in_flight = len(items)
        return {"job_id": job_id, "race_id": STATE.race_id, "label": label,
                "items": items}
    return None


def finish_job(job_id: str, keep_remaining: bool) -> None:
    """Close a job. Caller holds STATE.cond."""
    job = STATE.jobs.pop(job_id, None)
    if not job:
        return
    worker = STATE.workers.get(job["worker_id"])
    if worker and worker.job_id == job_id:
        worker.job_id = None
        worker.in_flight = 0
    remaining = [i for i in job["items"] if i["relpath"] not in STATE.done_set]
    if keep_remaining and remaining:
        STATE.pending.appendleft(remaining)
        STATE.cond.notify_all()
    if STATE.is_complete() and STATE.finished_at is None:
        STATE.finished_at = time.time()
        STATE.note_event("race", f"race complete: {len(STATE.done_set)} files in "
                                 f"{race_dir(STATE.label or '')}")


def watchdog() -> None:
    """Requeue units from workers that stopped making progress."""
    while True:
        time.sleep(5)
        now = time.time()
        with STATE.cond:
            stale = [j for j in STATE.jobs.values()
                     if now - j["last_progress"] > UNIT_STALE_SEC]
            for job in stale:
                STATE.requeues += 1
                STATE.note_event("requeue", f"{job['worker_id']} stalled on job "
                                            f"{job['job_id']}, returning work")
                finish_job(job["job_id"], keep_remaining=True)
            for worker in STATE.workers.values():
                worker.trim(now)
            if STATE.is_complete() and STATE.finished_at is None:
                STATE.finished_at = now


# --------------------------------------------------------------------------- #
# status payload
# --------------------------------------------------------------------------- #
def status_payload() -> dict:
    now = time.time()
    with STATE.cond:
        done = len(STATE.done_set)
        pending = STATE.pending_count()
        in_flight = STATE.in_flight_count()
        total = max(STATE.total_items, done + pending + in_flight)
        fpm = STATE.fpm(now)
        remaining = pending + in_flight
        eta = remaining / fpm * 60.0 if fpm > 0.01 and remaining else None
        workers = []
        for worker in sorted(STATE.workers.values(), key=lambda w: w.worker_id):
            workers.append({
                "worker_id": worker.worker_id,
                "status": worker.status(now),
                "files_done": worker.files_done,
                "units_done": worker.units_done,
                "in_flight": worker.in_flight,
                "job_id": worker.job_id,
                "last_seen_ago": round(now - worker.last_seen, 1),
                "epm": round(worker.epm(now), 1),
                "oro_429_count": worker.oro_429,
                "bytes": worker.bytes,
            })
        return {
            "now": now,
            "race_id": STATE.race_id,
            "label": STATE.label,
            "phase": STATE.phase,
            "complete": STATE.is_complete(),
            "started_at": STATE.started_at,
            "finished_at": STATE.finished_at,
            "metadata": dict(STATE.meta, oro_429=STATE.metadata_429),
            "totals": {
                "done": done,
                "preexisting": STATE.preexisting,
                "pending": pending,
                "in_flight": in_flight,
                "remaining": remaining,
                "total": total,
                "percent": round(done * 100.0 / total, 2) if total else 0.0,
            },
            "rate": {
                "files_per_min": round(fpm, 1),
                "eta_seconds": round(eta) if eta else None,
            },
            "oro_429_total": STATE.oro_429_total,
            "requeues": STATE.requeues,
            "steals": STATE.steals,
            "data_dir": str(DATA_DIR),
            "workers": workers,
            "events": list(STATE.events)[-20:],
        }


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8", ".json": "application/json",
    ".svg": "image/svg+xml", ".ico": "image/x-icon", ".map": "application/json",
    ".woff": "font/woff", ".woff2": "font/woff2", ".png": "image/png",
}

NO_DIST_PAGE = (b"<h1>oro-main</h1><p>API is up. The React build is missing - run "
                b"<code>cd frontend &amp;&amp; npm install &amp;&amp; npm run build</code>"
                b", or use the CLI.</p>")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "oro-main/1.0"

    # -- plumbing ---------------------------------------------------------- #
    def log_message(self, fmt, *args):  # quieter pm2 logs
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _forbid(self, ip: str, why: str, detail: str) -> bool:
        note_rejected(ip, self.path, why)
        self.close_connection = True  # do not read a body from a blocked host
        self._send(403, f"403 - {detail}\n".encode(), "text/plain; charset=utf-8")
        return False

    def _worker_gate(self) -> bool:
        """Worker routes: IP allowlist, plus the token when one is configured.
        These are the routes that write into the dataset."""
        allowed, ip = ip_allowed(self.client_address[0],
                                 self.headers.get("X-Forwarded-For"), ALLOW_NETS)
        if not allowed:
            return self._forbid(ip, "not in ALLOWED_IPS",
                                f"{ip} is not in ALLOWED_IPS on this server")
        if not self._authorized():
            self.close_connection = True
            self._json(401, {"error": "bad or missing X-Cluster-Token"})
            return False
        return True

    def _ui_gate(self, mutating: bool = False) -> bool:
        """Dashboard and read-only routes: optional IP allowlist, then the
        UI_USER/UI_PASSWORD login. This port may be public, so a mutating route
        still needs the token when no UI password is configured."""
        allowed, ip = ip_allowed(self.client_address[0],
                                 self.headers.get("X-Forwarded-For"), UI_ALLOW_NETS)
        if not allowed:
            return self._forbid(ip, "not in UI_ALLOWED_IPS",
                                f"{ip} is not in UI_ALLOWED_IPS on this server")
        if UI_USER:
            if basic_auth_ok(self.headers.get("Authorization")):
                # correct credentials always win, so a flood of wrong guesses
                # from your own IP or NAT cannot lock you out
                note_login_success(ip)
            else:
                throttled = login_banned(ip)
                note_login_failure(ip)
                self.close_connection = True
                if throttled:
                    time.sleep(0.5)  # slow down guessing
                    self._send(429, b"429 - too many failed logins, try again later\n",
                               "text/plain; charset=utf-8")
                    return False
                body = b"401 - dashboard login required\n"
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="oro-main", charset="UTF-8"')
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return False
        elif mutating and not self._authorized():
            # no dashboard password set: fall back to the cluster token
            self.close_connection = True
            self._json(401, {"error": "set UI_USER/UI_PASSWORD or send X-Cluster-Token"})
            return False
        return True

    def _authorized(self) -> bool:
        if not CLUSTER_TOKEN:
            return True
        return self.headers.get("X-Cluster-Token", "") == CLUSTER_TOKEN

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        chunks = []
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _worker(self, worker_id: str) -> Worker:
        worker = STATE.workers.get(worker_id)
        if worker is None:
            worker = Worker(worker_id)
            STATE.workers[worker_id] = worker
            STATE.note_event("worker", f"{worker_id} connected")
        worker.last_seen = time.time()
        return worker

    # -- routes ------------------------------------------------------------ #
    def do_GET(self):
        path, _, query = self.path.partition("?")
        args = urllib.parse.parse_qs(query)
        if path == "/api/health":  # intentionally open, for uptime checks
            return self._json(200, {"ok": True, "phase": STATE.phase,
                                    "race_id": STATE.race_id, "label": STATE.label})
        if path == "/api/work":
            if not self._worker_gate():
                return
            return self.handle_work(args.get("worker_id", [""])[0])
        if not self._ui_gate():
            return
        if path == "/api/status":
            return self._json(200, status_payload())
        if path == "/api/workers":
            return self._json(200, {"workers": status_payload()["workers"]})
        if path.startswith("/api/"):
            return self._json(404, {"error": "no such route"})
        return self.serve_static(path)

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        path, _, query = self.path.partition("?")
        if path == "/api/races":
            if not self._ui_gate(mutating=True):
                return
            try:
                payload = json.loads(self._body() or b"{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": "invalid JSON"})
            try:
                result = start_race(str(payload.get("race_id", "")),
                                    (payload.get("label") or "").strip() or None)
            except UnsafePath as err:
                return self._json(400, {"error": str(err)})
            except RuntimeError as err:
                return self._json(409, {"error": str(err)})
            except Exception as err:  # noqa: BLE001
                return self._json(502, {"error": f"could not start: {err}"})
            return self._json(200, result)

        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "work":
            if not self._worker_gate():
                return
            return self.handle_job_post(parts[2], parts[3])
        return self._json(404, {"error": "no such route"})

    def do_PUT(self):
        path, _, query = self.path.partition("?")
        if path != "/api/upload":
            return self._json(404, {"error": "no such route"})
        if not self._worker_gate():
            return
        args = urllib.parse.parse_qs(query)
        label = args.get("label", [""])[0]
        relpath = args.get("relpath", [""])[0]
        worker_id = args.get("worker_id", [""])[0] or "unknown"
        job_id = args.get("job_id", [""])[0]
        try:
            dest = safe_dest(label, relpath)
        except UnsafePath as err:
            return self._json(400, {"error": f"rejected path: {err}"})
        body = self._body()
        if not body:
            return self._json(400, {"error": "empty body"})

        created = True
        if exists_nonempty(dest):
            created = False  # idempotent resume
        else:
            write_atomic(dest, body)
        now = time.time()
        with STATE.cond:
            worker = self._worker(worker_id)
            first = STATE.mark_done(relpath, worker, len(body), now)
            job = STATE.jobs.get(job_id)
            if job:
                job["last_progress"] = now
                worker.in_flight = max(len(job["items"]) -
                                       sum(1 for i in job["items"]
                                           if i["relpath"] in STATE.done_set), 0)
        return self._json(201 if created else 200,
                          {"ok": True, "created": created, "counted": first,
                           "bytes": len(body)})

    # -- handlers ---------------------------------------------------------- #
    def handle_work(self, worker_id: str):
        if not worker_id:
            return self._json(400, {"error": "worker_id required"})
        deadline = time.monotonic() + LONGPOLL_SEC
        with STATE.cond:
            worker = self._worker(worker_id)
            while True:
                if STATE.phase in ("metadata", "episodes"):
                    unit = take_unit(worker)  # may steal a tail if the queue is empty
                    if unit:
                        return self._json(200, unit)
                left = deadline - time.monotonic()
                if left <= 0:
                    worker.last_seen = time.time()
                    return self._send(204, b"", "application/json")
                STATE.cond.wait(min(left, 5.0))
                worker.last_seen = time.time()

    def handle_job_post(self, job_id: str, action: str):
        if action not in ("progress", "done", "fail"):
            return self._json(404, {"error": "no such route"})
        try:
            payload = json.loads(self._body() or b"{}")
        except json.JSONDecodeError:
            payload = {}
        now = time.time()
        with STATE.cond:
            job = STATE.jobs.get(job_id)
            worker_id = payload.get("worker_id") or (job or {}).get("worker_id") or "unknown"
            worker = self._worker(worker_id)
            # an empty heartbeat keeps the worker online but must NOT count as
            # progress, or a stalled unit would never be requeued; a worker
            # parked on a 429 is throttled, not stalled
            paused_sec = float(payload.get("paused_sec") or 0)
            worker.paused_until = now + paused_sec if paused_sec > 0 else 0.0
            alive = (bool(payload.get("done_relpaths"))
                     or paused_sec > 0
                     or action != "progress")
            if job and alive:
                job["last_progress"] = now
            for relpath in payload.get("done_relpaths") or []:
                STATE.mark_done(relpath, worker, 0, now)
            extra_bytes = int(payload.get("bytes") or 0)
            if extra_bytes:
                worker.bytes += extra_bytes
            new_429 = int(payload.get("new_429") or 0)
            if new_429:
                worker.oro_429 += new_429
                STATE.oro_429_total += new_429
                STATE.note_event("rate-limit",
                                 f"{worker_id} hit {new_429} rate limit(s) on ORO")
            if job:
                worker.in_flight = max(len(job["items"]) -
                                       sum(1 for i in job["items"]
                                           if i["relpath"] in STATE.done_set), 0)
            if action == "progress":
                return self._json(200, {"ok": True, "known_job": bool(job),
                                        "abandon": job is None,
                                        "dropped": (job or {}).get("dropped", [])})
            if job is None:
                return self._json(200, {"ok": True, "known_job": False})
            if action == "done":
                worker.units_done += 1
                finish_job(job_id, keep_remaining=True)
            else:
                STATE.note_event("fail", f"{worker_id} failed job {job_id}: "
                                         f"{payload.get('error', 'unknown')}")
                finish_job(job_id, keep_remaining=True)
            return self._json(200, {"ok": True})

    def serve_static(self, path: str):
        if not DIST_DIR.exists():
            return self._send(200, NO_DIST_PAGE, "text/html; charset=utf-8")
        rel = urllib.parse.unquote(path).lstrip("/") or "index.html"
        try:
            candidate = DIST_DIR.joinpath(*[p for p in rel.split("/") if p not in ("", ".", "..")])
            candidate.resolve().relative_to(DIST_DIR)
        except ValueError:
            candidate = DIST_DIR / "index.html"
        if not candidate.is_file():
            candidate = DIST_DIR / "index.html"
        if not candidate.is_file():
            return self._send(404, b"not found", "text/plain")
        ctype = CONTENT_TYPES.get(candidate.suffix, "application/octet-stream")
        return self._send(200, candidate.read_bytes(), ctype)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def api_call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    host = "127.0.0.1" if BIND_HOST in ("0.0.0.0", "::", "") else BIND_HOST
    url = f"http://{host}:{BIND_PORT}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if CLUSTER_TOKEN:
        headers["X-Cluster-Token"] = CLUSTER_TOKEN
    if UI_USER:  # POST /api/races is a dashboard route
        creds = base64.b64encode(f"{UI_USER}:{UI_PASSWORD}".encode()).decode()
        headers["Authorization"] = f"Basic {creds}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read()
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as err:
        body = err.read()
        try:
            return err.code, json.loads(body)
        except json.JSONDecodeError:
            return err.code, {"error": body.decode(errors="replace")}
    except urllib.error.URLError as err:
        print(f"cannot reach oro-main at {url}: {err}\n"
              f"start it with: pm2 start ecosystem.main.cjs   (or python oro_main.py serve)")
        sys.exit(1)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    # every worker holds a long-poll open and uploads on separate connections,
    # so the default backlog of 5 is far too small
    request_queue_size = 128


def cmd_serve(_args) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=watchdog, daemon=True, name="watchdog").start()
    server = Server((BIND_HOST, BIND_PORT), Handler)
    worker_gates = []
    if ALLOW_NETS:
        worker_gates.append(f"ip-allowlist({len(ALLOW_NETS)}+loopback)")
    if CLUSTER_TOKEN:
        worker_gates.append("token")
    ui_gates = []
    if UI_ALLOW_NETS:
        ui_gates.append(f"ip-allowlist({len(UI_ALLOW_NETS)}+loopback)")
    if UI_USER:
        ui_gates.append(f"login({UI_USER})")
    elif CLUSTER_TOKEN:
        ui_gates.append("token-to-start-a-race")
    log(f"serving on http://{BIND_HOST}:{BIND_PORT}  data={DATA_DIR}  "
        f"ui={'dist' if DIST_DIR.exists() else 'not built'}")
    log(f"access: workers={'+'.join(worker_gates) if worker_gates else 'OPEN'}  "
        f"dashboard={'+'.join(ui_gates) if ui_gates else 'OPEN'}")
    if ALLOW_NETS:
        log("worker IPs: " + ", ".join(str(n) for n in ALLOW_NETS))
    if UI_ALLOW_NETS:
        log("dashboard IPs: " + ", ".join(str(n) for n in UI_ALLOW_NETS))
    public = BIND_HOST not in ("127.0.0.1", "localhost", "::1")
    if public and not worker_gates:
        log("WARNING: no ALLOWED_IPS and no CLUSTER_TOKEN - anyone who can reach "
            f"{BIND_HOST}:{BIND_PORT} can upload files into your data dir. "
            "Set ALLOWED_IPS in .env.")
    if public and not ui_gates:
        log("WARNING: the dashboard is open to anyone who can reach "
            f"{BIND_HOST}:{BIND_PORT}, including starting races. "
            "Set UI_USER and UI_PASSWORD in .env.")
    if public and UI_USER and not TRUST_PROXY:
        log("NOTE: the dashboard login is sent base64-encoded over plain HTTP. "
            "Put main behind an HTTPS reverse proxy if the port is public.")
    if TRUST_PROXY:
        log("TRUST_PROXY=1: the last X-Forwarded-For entry from a loopback/private "
            "peer is treated as the client IP")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")


def cmd_start(args) -> None:
    code, body = api_call("POST", "/api/races",
                          {"race_id": args.race_id, "label": args.label})
    print(json.dumps(body, indent=2))
    sys.exit(0 if code == 200 else 1)


def cmd_status(_args) -> None:
    code, s = api_call("GET", "/api/status")
    if code != 200:
        print(json.dumps(s, indent=2))
        sys.exit(1)
    t, r = s["totals"], s["rate"]
    eta = f"{r['eta_seconds'] // 60}m{r['eta_seconds'] % 60:02d}s" if r["eta_seconds"] else "-"
    print(f"race   : {s['race_id']}  label={s['label']}  phase={s['phase']}"
          f"{'  COMPLETE' if s['complete'] else ''}")
    m = s["metadata"]
    print(f"meta   : agents {m['agents_done']}/{m['agents_total']}  "
          f"runs {m['runs_saved']}/{m['runs_included']}  episodes {m['episodes_found']}")
    print(f"files  : done {t['done']}/{t['total']} ({t['percent']}%)  "
          f"pending {t['pending']}  in-flight {t['in_flight']}  "
          f"pre-existing {t['preexisting']}")
    print(f"rate   : {r['files_per_min']} files/min  eta {eta}  "
          f"429s {s['oro_429_total']}  requeues {s['requeues']}  steals {s['steals']}")
    print(f"{'worker':<22}{'status':<13}{'done':>7}{'units':>7}{'flight':>7}"
          f"{'epm':>8}{'429':>5}{'MB':>9}{'seen':>7}")
    for w in s["workers"]:
        print(f"{w['worker_id'][:21]:<22}{w['status']:<13}{w['files_done']:>7}"
              f"{w['units_done']:>7}{w['in_flight']:>7}{w['epm']:>8.1f}"
              f"{w['oro_429_count']:>5}{w['bytes'] / 1e6:>9.1f}"
              f"{w['last_seen_ago']:>6.0f}s")


def main() -> None:
    parser = argparse.ArgumentParser(description="oro-main coordinator")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run API + UI server").set_defaults(func=cmd_serve)
    start = sub.add_parser("start", help="start a race on the running server")
    start.add_argument("--race-id", required=True)
    start.add_argument("--label", default=None, help="default: race<race_number>")
    start.set_defaults(func=cmd_start)
    sub.add_parser("status", help="print cluster status").set_defaults(func=cmd_status)
    args = parser.parse_args()
    socket.setdefaulttimeout(None)
    args.func(args)


if __name__ == "__main__":
    main()
