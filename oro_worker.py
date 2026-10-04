#!/usr/bin/env python3
"""oro-worker: pulls small download units from oro-main and fetches raw ORO logs.

Runs on the main VPS (WRITE_LOCAL=1, writes straight to DATA_DIR) and on every
extra VPS (WRITE_LOCAL=0, uploads each file to main the moment it is downloaded).
Never listens on a port: it only makes outbound calls to MAIN_URL and to the ORO
API on 443. Same file works on Linux and Windows.

Env: MAIN_URL, CLUSTER_TOKEN, WORKER_ID, WRITE_LOCAL, DATA_DIR, WORKER_THREADS
"""

from __future__ import annotations

import collections
import json
import os
import platform
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_env_file(path: Path) -> None:
    """Read KEY=VALUE lines from .env next to this script. The real environment
    always wins, so `VAR=x pm2 restart oro-worker --update-env` still overrides."""
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

# on main the local worker needs no MAIN_URL: it follows the port main binds
MAIN_URL = (os.environ.get("MAIN_URL")
            or f"http://127.0.0.1:{os.environ.get('BIND_PORT', '8080')}").rstrip("/")
CLUSTER_TOKEN = os.environ.get("CLUSTER_TOKEN", "")
WORKER_ID = os.environ.get("WORKER_ID") or platform.node() or "worker"
WRITE_LOCAL = os.environ.get("WRITE_LOCAL", "0").strip() in ("1", "true", "yes")
DATA_DIR = Path(os.environ.get("DATA_DIR") or (ROOT / "Data" / "races")).resolve()
THREADS = max(1, int(os.environ.get("WORKER_THREADS", "4")))
MAX_RPM = int(os.environ.get("ORO_MAX_RPM", "98"))
PROGRESS_INTERVAL = float(os.environ.get("PROGRESS_INTERVAL", "1.0"))
HEARTBEAT_SEC = float(os.environ.get("HEARTBEAT_SEC", "10"))
USER_AGENT = "oro-race-cluster-worker/1.0"


def log(msg: str) -> None:
    print(f"[{WORKER_ID} {time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# rate limiter: ONE per process, ORO requests only (uploads to main are free)
# --------------------------------------------------------------------------- #
class RateLimiter:
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
        with self._cond:
            until = time.monotonic() + retry_after + 15.0
            self._blocked_until = max(self._blocked_until, until)
            self._window.clear()
            self._cond.notify_all()
            return self._blocked_until - time.monotonic()

    def pause_remaining(self) -> float:
        """Seconds left in a 429 penalty, so main can tell paused from stalled."""
        with self._cond:
            return max(0.0, self._blocked_until - time.monotonic())


LIMITER = RateLimiter()


def parse_retry_after(value: str | None) -> float:
    try:
        return max(float(value), 1.0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 60.0


# --------------------------------------------------------------------------- #
# shared per-job counters reported to main
# --------------------------------------------------------------------------- #
class Report:
    def __init__(self):
        self.lock = threading.Lock()
        self.buffer: list[str] = []
        self.bytes = 0
        self.new_429 = 0
        self.abandon = False
        self.dropped: set[str] = set()  # tail items main handed to another worker

    def file_done(self, relpath: str, nbytes: int) -> None:
        with self.lock:
            self.buffer.append(relpath)
            self.bytes += nbytes

    def hit_429(self) -> None:
        with self.lock:
            self.new_429 += 1

    def drain(self) -> dict | None:
        with self.lock:
            if not self.buffer and not self.new_429:
                return None
            payload = {"worker_id": WORKER_ID, "done_relpaths": self.buffer,
                       "bytes": self.bytes, "new_429": self.new_429}
            self.buffer = []
            self.bytes = 0
            self.new_429 = 0
            return payload


REPORT = Report()


# --------------------------------------------------------------------------- #
# HTTP to main (no rate limit) and to ORO (rate limited)
# --------------------------------------------------------------------------- #
def main_request(method: str, path: str, data: bytes | None = None,
                 ctype: str = "application/json", timeout: float = 120.0):
    headers = {"User-Agent": USER_AGENT}
    if CLUSTER_TOKEN:
        headers["X-Cluster-Token"] = CLUSTER_TOKEN
    if data is not None:
        headers["Content-Type"] = ctype
    req = urllib.request.Request(MAIN_URL + path, data=data, headers=headers,
                                 method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        return resp.status, body


def oro_get(url: str, attempts: int = 8, timeout: float = 120.0) -> bytes:
    last_err: Exception | None = None
    for attempt in range(attempts):
        LIMITER.acquire()
        req = urllib.request.Request(
            url, headers={"Accept": "application/json", "User-Agent": USER_AGENT}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as err:
            err.read()
            if err.code == 429:
                wait = LIMITER.penalize(parse_retry_after(err.headers.get("Retry-After")))
                REPORT.hit_429()
                log(f"429 from ORO: all ORO threads paused {wait:.0f}s")
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


# --------------------------------------------------------------------------- #
# destination paths (local mode only)
# --------------------------------------------------------------------------- #
SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def local_dest(label: str, relpath: str) -> Path:
    if not label or not SAFE_SEGMENT.match(label):
        raise ValueError(f"bad label: {label!r}")
    base = DATA_DIR / label
    parts = []
    for part in (relpath or "").replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if not SAFE_SEGMENT.match(part):
            raise ValueError(f"bad relpath segment {part!r} in {relpath!r}")
        parts.append(part)
    if not parts:
        raise ValueError("empty relpath")
    dest = base.joinpath(*parts)
    dest.parent.mkdir(parents=True, exist_ok=True)
    return dest


def exists_nonempty(path: Path) -> bool:
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# one item: download raw bytes, then either write locally or upload immediately
# --------------------------------------------------------------------------- #
def upload(label: str, relpath: str, job_id: str, data: bytes) -> None:
    """Keep retrying until main has the bytes. Never give up silently."""
    qs = urllib.parse.urlencode({"label": label, "relpath": relpath,
                                 "worker_id": WORKER_ID, "job_id": job_id})
    attempt = 0
    while True:
        attempt += 1
        try:
            status, _ = main_request("PUT", f"/api/upload?{qs}", data=data,
                                     ctype="application/octet-stream")
            if status in (200, 201):
                return
            log(f"upload of {relpath} got HTTP {status}, retrying")
        except urllib.error.HTTPError as err:
            body = err.read().decode(errors="replace")[:200]
            if err.code in (400, 401):
                raise RuntimeError(f"upload rejected ({err.code}): {body}") from None
            log(f"upload error {err.code} for {relpath}: {body}")
        except (urllib.error.URLError, OSError, TimeoutError) as err:
            log(f"main unreachable while uploading {relpath}: {err}")
        time.sleep(min(2 ** min(attempt, 5), 30))


def handle_item(item: dict, label: str, job_id: str) -> None:
    relpath, url = item["relpath"], item["url"]
    dest = local_dest(label, relpath) if WRITE_LOCAL else None

    if dest is not None and exists_nonempty(dest):
        REPORT.file_done(relpath, 0)  # resume: already on disk
        return

    data = oro_get(url)
    if not data:
        raise RuntimeError(f"empty body for {relpath}")

    if dest is not None:
        tmp = dest.with_name(dest.name + ".partial")
        tmp.write_bytes(data)
        os.replace(tmp, dest)
        REPORT.file_done(relpath, len(data))
    else:
        # remotes hold the bytes in memory only; nothing is left on this disk
        upload(label, relpath, job_id, data)
        # main counts uploaded bytes itself, so don't report them twice
        REPORT.file_done(relpath, 0)


# --------------------------------------------------------------------------- #
# job execution
# --------------------------------------------------------------------------- #
def progress_reporter(job_id: str, stop: threading.Event) -> None:
    last_post = 0.0
    while not stop.is_set():
        stop.wait(PROGRESS_INTERVAL)
        payload = REPORT.drain()
        now = time.monotonic()
        if not payload:
            # keep heartbeating while parked on a 429, otherwise main would call
            # this worker offline and requeue a unit it is still going to finish
            if now - last_post < HEARTBEAT_SEC:
                continue
            payload = {"worker_id": WORKER_ID, "done_relpaths": [], "bytes": 0,
                       "new_429": 0}
        payload["paused_sec"] = round(LIMITER.pause_remaining(), 1)
        last_post = now
        try:
            _, body = main_request("POST", f"/api/work/{job_id}/progress",
                                   data=json.dumps(payload).encode(), timeout=30)
            reply = json.loads(body or b"{}")
            if reply.get("dropped"):
                with REPORT.lock:
                    REPORT.dropped.update(reply["dropped"])
            if reply.get("abandon"):
                REPORT.abandon = True
                stop.set()
        except Exception as err:  # noqa: BLE001 - progress is best effort
            log(f"progress post failed: {err}")
            with REPORT.lock:  # put the relpaths back so main still learns of them
                REPORT.buffer = payload["done_relpaths"] + REPORT.buffer
                REPORT.bytes += payload["bytes"]
                REPORT.new_429 += payload["new_429"]


def run_job(job: dict) -> None:
    job_id, label = job["job_id"], job["label"]
    items = job.get("items") or []
    log(f"job {job_id}: {len(items)} episodes -> "
        f"{'local disk' if WRITE_LOCAL else 'upload to main'}")

    work: queue.Queue = queue.Queue()
    for item in items:
        work.put(item)
    errors: list[str] = []
    errors_lock = threading.Lock()
    stop = threading.Event()
    reporter = threading.Thread(target=progress_reporter, args=(job_id, stop),
                                daemon=True)
    reporter.start()

    def consume() -> None:
        while not REPORT.abandon:
            try:
                item = work.get_nowait()
            except queue.Empty:
                return
            if item["relpath"] in REPORT.dropped:
                work.task_done()  # main gave this tail item to a faster worker
                continue
            try:
                handle_item(item, label, job_id)
            except Exception as err:  # noqa: BLE001 - one bad episode must not kill the job
                with errors_lock:
                    errors.append(f"{item['relpath']}: {err}")
                log(f"item failed {item['relpath']}: {err}")
            finally:
                work.task_done()

    threads = [threading.Thread(target=consume, daemon=True)
               for _ in range(min(THREADS, max(len(items), 1)))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    stop.set()
    reporter.join(timeout=5)
    final = REPORT.drain() or {"worker_id": WORKER_ID, "done_relpaths": [],
                               "bytes": 0, "new_429": 0}

    REPORT.dropped.clear()
    if REPORT.abandon:
        REPORT.abandon = False
        log(f"job {job_id} was requeued by main, dropping it")
        return

    action = "fail" if errors else "done"
    final["error"] = "; ".join(errors[:3]) if errors else None
    try:
        main_request("POST", f"/api/work/{job_id}/{action}",
                     data=json.dumps(final).encode(), timeout=60)
    except Exception as err:  # noqa: BLE001
        log(f"could not report {action} for {job_id}: {err}")
    log(f"job {job_id} {action}"
        f"{f' ({len(errors)} item errors, returned to queue)' if errors else ''}")


def fetch_work() -> dict | None:
    qs = urllib.parse.urlencode({"worker_id": WORKER_ID})
    status, body = main_request("GET", f"/api/work?{qs}", timeout=90)
    if status == 204 or not body:
        return None
    return json.loads(body)


def main() -> None:
    log(f"waiting for work from {MAIN_URL}  write_local={int(WRITE_LOCAL)}  "
        f"threads={THREADS}  limit={MAX_RPM}/min"
        + (f"  data={DATA_DIR}" if WRITE_LOCAL else ""))
    if not WRITE_LOCAL and not os.environ.get("MAIN_URL"):
        log("WARNING: MAIN_URL is not set, so this worker is asking the local "
            "machine for work. Set MAIN_URL in .env to main's address.")
    backoff = 1.0
    while True:
        try:
            job = fetch_work()
            backoff = 1.0
        except urllib.error.HTTPError as err:
            body = err.read().decode(errors="replace")[:200]
            log(f"main said {err.code}: {body}")
            if err.code == 401:
                log("CLUSTER_TOKEN does not match main - fix env and restart")
            time.sleep(min(backoff * 2, 30))
            backoff = min(backoff * 2, 30)
            continue
        except (urllib.error.URLError, OSError, TimeoutError) as err:
            log(f"main unreachable ({err}); retrying in {backoff:.0f}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        except json.JSONDecodeError as err:
            log(f"bad work payload: {err}")
            time.sleep(2)
            continue

        if job is None:
            continue  # 204 / long-poll timeout: still waiting
        try:
            run_job(job)
        except Exception as err:  # noqa: BLE001 - never exit the wait loop
            log(f"job crashed: {err}")
            time.sleep(2)


if __name__ == "__main__":
    main()
