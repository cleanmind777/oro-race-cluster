import React, { useEffect, useRef, useState } from "react";
import { getStatus, getToken, setToken, startRace } from "./api.js";

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
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [token, setTok] = useState(getToken());

  const running =
    status && ["metadata", "episodes"].includes(status.phase) && !status.complete;

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

  return (
    <form className="panel start" onSubmit={submit}>
      <div className="row">
        <label>
          race_id
          <input
            value={raceId}
            onChange={(e) => setRaceId(e.target.value)}
            placeholder="51abddf9-0ea6-4afb-9b19-bd0bdc182af6"
            spellCheck={false}
          />
        </label>
        <label className="narrow">
          label <span className="hint">(default race&lt;number&gt;)</span>
          <input
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder="race2"
            spellCheck={false}
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
        <button type="submit" disabled={busy || !raceId.trim()}>
          {busy ? "Starting..." : running ? "Race running" : "Start race"}
        </button>
      </div>
      {error && <div className="error">{error}</div>}
      {running && (
        <div className="hint">
          A race is already running. Starting the same race_id again after it ends
          skips files that are already on disk.
        </div>
      )}
    </form>
  );
}

function PhaseBar({ status }) {
  const meta = status.metadata;
  const totals = status.totals;
  const metaPct = meta.agents_total
    ? Math.round((meta.agents_done / meta.agents_total) * 100)
    : 0;
  const metaDone = status.phase !== "metadata" && status.phase !== "idle";

  return (
    <div className="panel phases">
      <div className="phase">
        <div className="phase-head">
          <span className={`dot ${status.phase === "metadata" ? "live" : metaDone ? "ok" : ""}`} />
          <strong>1. metadata</strong>
          <span className="hint">
            qualifiers {meta.agents_done}/{meta.agents_total || "?"} &middot; included
            RACE runs {meta.runs_saved}/{meta.runs_included} &middot; episodes found{" "}
            {meta.episodes_found}
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

function WorkerTable({ status }) {
  const workers = status.workers || [];
  const maxEpm = Math.max(1, ...workers.map((w) => w.epm));
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
      <table>
        <thead>
          <tr>
            <th>worker_id</th>
            <th>status</th>
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
        VPS is parked on an ORO 429 and will resume on its own.
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
      {status && <WorkerTable status={status} />}
      {status && <Events status={status} />}
    </div>
  );
}
