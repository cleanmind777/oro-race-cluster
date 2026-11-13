import React, { useEffect, useRef, useState } from "react";
import {
  getStatus,
  getToken,
  pauseAllWorkers,
  pauseWorker,
  resumeAllWorkers,
  resumeWorker,
  setToken,
  startQualifying,
  startRace,
  stopRace,
} from "./api.js";

const POLL_MS = 1500;

function fmtBytes(n) {
  if (!n) return "0";
  if (n > 1e9) return `${(n / 1e9).toFixed(2)} GB`;
  if (n > 1e6) return `${(n / 1e6).toFixed(1)} MB`;
  return `${(n / 1e3).toFixed(0)} kB`;
}

function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined) return "-";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s}s`;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m ${s % 60}s`;
}

function fmtClock(ts) {
  return ts ? new Date(ts * 1000).toLocaleTimeString() : "-";
}

function StartRaceForm({ status, onStarted }) {
  const [raceId, setRaceId] = useState("");
  const [label, setLabel] = useState("");
  const [minScore, setMinScore] = useState("0.55");
  const [qualLabel, setQualLabel] = useState("qualifying");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [token, setTok] = useState(getToken());

  const running =
    status &&
    ["waiting", "metadata", "episodes"].includes(status.phase) &&
    !status.complete;
  const isQualifying = status && status.race_id === "qualifying";

  async function submit(event) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      await startRace(raceId, label);
      onStarted();
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setBusy(false);
    }
  }

  async function submitQualifying(event) {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      await startQualifying(minScore, qualLabel);
      onStarted();
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setBusy(false);
    }
  }

  async function stop() {
    if (!window.confirm("Stop this download? Files already downloaded are kept."))
      return;
    setError("");
    setBusy(true);
    try {
      await stopRace();
      onStarted();
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <form className="panel start" onSubmit={submit}>
        <div className="row">
          <label>
            race_id
            <input
              value={raceId}
              onChange={(e) => setRaceId(e.target.value)}
              placeholder="51abddf9-0ea6-4afb-9b19-bd0bdc182af6"
              spellCheck={false}
              disabled={running}
            />
          </label>
          <label className="narrow">
            label <span className="hint">(default race&lt;number&gt;)</span>
            <input
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="race2"
              spellCheck={false}
              disabled={running}
            />
          </label>
          <label className="narrow">
            cluster token <span className="hint">(only if main sets one)</span>
            <input
              type="password"
              value={token}
              onChange={(e) => {
                setTok(e.target.value);
                setToken(e.target.value);
              }}
              placeholder="leave empty if unused"
            />
          </label>
          <button type="submit" disabled={busy || running || !raceId.trim()}>
            {busy
              ? "Starting..."
              : status && status.phase === "waiting"
              ? "Waiting for publication"
              : running
              ? "Download running"
              : "Start race"}
          </button>
          {running && (
            <button type="button" className="danger" onClick={stop} disabled={busy}>
              Stop
            </button>
          )}
        </div>
      </form>
      <form className="panel start" onSubmit={submitQualifying}>
        <div className="row">
          <label className="narrow">
            min final_score
            <input
              value={minScore}
              onChange={(e) => setMinScore(e.target.value)}
              placeholder="0.55"
              spellCheck={false}
              disabled={running}
            />
          </label>
          <label className="narrow">
            qualifying label
            <input
              value={qualLabel}
              onChange={(e) => setQualLabel(e.target.value)}
              placeholder="qualifying"
              spellCheck={false}
              disabled={running}
            />
          </label>
          <button type="submit" disabled={busy || running}>
            {busy
              ? "Starting..."
              : running && isQualifying
              ? "Qualifying running"
              : "Start qualifying"}
          </button>
        </div>
        <div className="hint">
          Downloads QUALIFYING episode logs for public-leaderboard agents with
          final_score ≥ the threshold (default 55%). Same cluster path as a race;
          written under DATA_DIR/&lt;label&gt;/.
        </div>
        {error && <div className="error">{error}</div>}
        {running && (
          <div className="hint">
            A download is already under way, and main picks it up again by itself
            if it restarts. Stopping keeps every file already downloaded;
            starting the same race_id or qualifying label later skips them.
          </div>
        )}
      </form>
    </>
  );
}

function WaitBar({ status }) {
  const watch = status.watch || {};
  const since = status.started_at
    ? Math.round((status.now - status.started_at) / 60)
    : 0;
  const isQualifying = status.race_id === "qualifying";
  return (
    <div className="panel phases">
      <div className="phase">
        <div className="phase-head">
          <span className="dot live" />
          <strong>
            {isQualifying ? "0. loading leaderboard" : "0. waiting for publication"}
          </strong>
          <span className="hint">
            {watch.note || "checking"}
            {!isQualifying && (
              <>
                {" "}
                &middot; checked {watch.checks || 0}&times;
                {watch.interval ? ` every ${Math.round(watch.interval)}s` : ""}
                {since ? ` · waiting ${since} min` : ""}
              </>
            )}
          </span>
        </div>
        <div className="bar">
          <div className="fill meta waiting" style={{ width: "100%" }} />
        </div>
      </div>
      <div className="hint">
        {isQualifying
          ? "Main is paging the public leaderboard for agents above the score threshold, then enqueues their QUALIFYING runs."
          : "ORO publishes episode logs some hours after a race ends. Main keeps checking and starts the cluster by itself, re-reading the metadata then in case it changed while waiting."}
      </div>
    </div>
  );
}

function PhaseBar({ status }) {
  const meta = status.metadata;
  const totals = status.totals;
  const metaPct = meta.files_total
    ? Math.round((meta.files_done / meta.files_total) * 100)
    : 0;
  const metaDone = !["metadata", "idle", "waiting"].includes(status.phase);

  if (status.phase === "waiting") return <WaitBar status={status} />;

  return (
    <div className="panel phases">
      <div className="phase">
        <div className="phase-head">
          <span className={`dot ${status.phase === "metadata" ? "live" : metaDone ? "ok" : ""}`} />
          <strong>1. metadata</strong>
          <span className="hint">
            files {meta.files_done}/{meta.files_total || "?"} &middot; agents{" "}
            {meta.agents_done}/{meta.agents_total || "?"} &middot; included{" "}
            {status.race_id === "qualifying" ? "QUALIFYING" : "RACE"} runs{" "}
            {meta.runs_saved}/{meta.runs_included} &middot; episodes found{" "}
            {meta.episodes_found}
            {meta.min_score != null ? ` · min_score ${meta.min_score}` : ""}
            {meta.oro_429 ? ` · ${meta.oro_429} 429s` : ""}
          </span>
        </div>
        <div className="bar">
          <div className="fill meta" style={{ width: `${metaDone ? 100 : metaPct}%` }} />
        </div>
      </div>
      <div className="phase">
        <div className="phase-head">
          <span className={`dot ${status.complete ? "ok" : totals.done ? "live" : ""}`} />
          <strong>2. episodes</strong>
          <span className="hint">
            {totals.done}/{totals.total} files &middot; {totals.percent}%
            {totals.preexisting ? ` (${totals.preexisting} already on disk)` : ""}
            {status.given_up ? ` · ${status.given_up} unavailable` : ""}
          </span>
        </div>
        <div className="bar">
          <div className="fill" style={{ width: `${totals.percent}%` }} />
          <div
            className="fill flight"
            style={{
              width: totals.total
                ? `${(totals.in_flight / totals.total) * 100}%`
                : "0%",
            }}
          />
        </div>
      </div>
    </div>
  );
}

function Totals({ status }) {
  const t = status.totals;
  const discovering = status.phase === "metadata";
  const cards = [
    ["done on main", t.done.toLocaleString()],
    [discovering ? "remaining (so far)" : "remaining", t.remaining.toLocaleString()],
    ["in flight", t.in_flight.toLocaleString()],
    ["files / min", status.rate.files_per_min],
    [discovering ? "ETA for known files" : "ETA", fmtDuration(status.rate.eta_seconds)],
    ["429s from ORO", status.oro_429_total],
    ["requeues", status.requeues],
  ];
  return (
    <div className="cards">
      {cards.map(([k, v]) => (
        <div className="card" key={k}>
          <div className="card-value">{v}</div>
          <div className="card-key">{k}</div>
        </div>
      ))}
    </div>
  );
}

function WorkerTable({ status, onChanged }) {
  const workers = status.workers || [];
  const maxEpm = Math.max(1, ...workers.map((w) => w.epm));
  const [busyId, setBusyId] = useState("");
  const [error, setError] = useState("");

  async function toggle(worker) {
    const id = worker.worker_id;
    setError("");
    setBusyId(id);
    try {
      if (worker.enabled === false) await resumeWorker(id);
      else await pauseWorker(id);
      onChanged && onChanged();
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setBusyId("");
    }
  }

  async function all(enabled) {
    setError("");
    setBusyId(enabled ? "__resume_all__" : "__pause_all__");
    try {
      if (enabled) await resumeAllWorkers();
      else await pauseAllWorkers();
      onChanged && onChanged();
    } catch (err) {
      setError(String(err.message || err));
    } finally {
      setBusyId("");
    }
  }

  if (!workers.length) {
    return (
      <div className="panel">
        <div className="hint">
          No workers yet. Start oro-worker under PM2 on each VPS with MAIN_URL set to
          this server (including the port); they will appear here while waiting.
        </div>
      </div>
    );
  }
  return (
    <div className="panel">
      <div className="row worker-controls">
        <button
          type="button"
          className="danger"
          disabled={!!busyId}
          onClick={() => all(false)}
        >
          {busyId === "__pause_all__" ? "Pausing..." : "Pause all"}
        </button>
        <button type="button" disabled={!!busyId} onClick={() => all(true)}>
          {busyId === "__resume_all__" ? "Resuming..." : "Resume all"}
        </button>
        <span className="hint">
          Pause stops new job assignment only; the process keeps polling. In-flight
          units may still finish.
        </span>
      </div>
      {error && <div className="error">{error}</div>}
      <table>
        <thead>
          <tr>
            <th>worker_id</th>
            <th>status</th>
            <th>control</th>
            <th className="num">files done</th>
            <th className="num">in flight</th>
            <th className="num">units</th>
            <th className="num">recent EPM</th>
            <th>share of current rate</th>
            <th className="num">429s</th>
            <th className="num">bytes on main</th>
            <th className="num">last seen</th>
          </tr>
        </thead>
        <tbody>
          {workers.map((w) => (
            <tr key={w.worker_id} className={w.status}>
              <td className="mono">{w.worker_id}</td>
              <td>
                <span className={`pill ${w.status}`}>{w.status}</span>
              </td>
              <td>
                <button
                  type="button"
                  className={w.enabled === false ? "" : "danger"}
                  disabled={!!busyId}
                  onClick={() => toggle(w)}
                >
                  {busyId === w.worker_id
                    ? "..."
                    : w.enabled === false
                    ? "Resume"
                    : "Pause"}
                </button>
              </td>
              <td className="num">{w.files_done.toLocaleString()}</td>
              <td className="num">{w.in_flight}</td>
              <td className="num">{w.units_done}</td>
              <td className="num">{w.epm.toFixed(1)}</td>
              <td>
                <div className="bar thin">
                  <div className="fill" style={{ width: `${(w.epm / maxEpm) * 100}%` }} />
                </div>
              </td>
              <td className="num">{w.oro_429_count}</td>
              <td className="num">{fmtBytes(w.bytes)}</td>
              <td className="num">{w.last_seen_ago.toFixed(0)}s</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="hint">
        EPM is measured over the last few minutes, so a busy VPS simply pulls fewer
        units. Nothing here is a fixed 1/N target. <code>rate-limited</code> means that
        VPS is parked on an ORO 429 and will resume on its own. <code>paused</code> is
        a dashboard off switch.
      </div>
    </div>
  );
}

function Events({ status }) {
  const events = [...(status.events || [])].reverse();
  if (!events.length) return null;
  return (
    <div className="panel events">
      {events.map((e, i) => (
        <div className="event" key={`${e.ts}-${i}`}>
          <span className="mono hint">{fmtClock(e.ts)}</span>
          <span className={`pill ${e.kind}`}>{e.kind}</span>
          <span>{e.msg}</span>
        </div>
      ))}
    </div>
  );
}

export default function App() {
  const [status, setStatus] = useState(null);
  const [error, setError] = useState("");
  const timer = useRef(null);

  async function refresh() {
    try {
      setStatus(await getStatus());
      setError("");
    } catch (err) {
      setError(`cannot reach oro-main: ${err.message || err}`);
    }
  }

  useEffect(() => {
    refresh();
    timer.current = setInterval(refresh, POLL_MS);
    return () => clearInterval(timer.current);
  }, []);

  return (
    <div className="app">
      <header>
        <h1>ORO race-log cluster</h1>
        <div className="hint">
          {status ? (
            <>
              race <span className="mono">{status.race_id || "none"}</span> &middot;
              label <span className="mono">{status.label || "-"}</span> &middot; phase{" "}
              <strong>{status.complete ? "complete" : status.phase}</strong> &middot;
              data <span className="mono">{status.data_dir}</span>
            </>
          ) : (
            "connecting..."
          )}
        </div>
      </header>

      {error && <div className="panel error">{error}</div>}
      <StartRaceForm status={status} onStarted={refresh} />
      {status && <PhaseBar status={status} />}
      {status && <Totals status={status} />}
      {status && <WorkerTable status={status} onChanged={refresh} />}
      {status && <Events status={status} />}
      <p className="flaticon-credit">
        <a
          href="https://www.flaticon.com/free-icons/quadrant"
          title="quadrant icons"
          target="_blank"
          rel="noreferrer"
        >
          Quadrant icons created by Jagat Icon - Flaticon
        </a>
      </p>
    </div>
  );
}
