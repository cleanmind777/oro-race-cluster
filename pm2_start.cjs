// PM2 only auto-detects config files named *.config.cjs / *.json / *.yaml, so
// `pm2 start ecosystem.main.cjs` runs the file as a plain Node script instead of
// reading its `apps` array. When that happens we hand the same app definitions to
// PM2 as a temporary JSON config and then remove the wrapper entry PM2 made for
// this file, so the documented command works as-is. If a PM2 version does read
// the file as a config this never runs: the file is required, not executed.
const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawn, spawnSync } = require("child_process");

// vars PM2 injects into the wrapper that must not leak into the real apps.
// PM2_HOME is the exception: it says which daemon to talk to, and dropping it
// would create the apps in a different one than `pm2 status` shows.
const PM2_VARS =
  /^(pm_|PM2_|PM_)|^(name|NODE_APP_INSTANCE|unique_id|exec_interpreter|instance_var|env_name)$/i;
const PM2_KEEP = /^PM2_HOME$/i;

const WIN = process.platform === "win32";

// Same line format as load_env_file() in the Python files.
function parseEnvFile(dir) {
  const vals = {};
  let text = "";
  try {
    text = fs.readFileSync(path.join(dir, ".env"), "utf8");
  } catch {
    return vals;
  }
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim().replace(/^export\s+/, "");
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq < 1) continue;
    const key = line.slice(0, eq).trim();
    let value = line.slice(eq + 1).trim();
    if (value[0] === '"' || value[0] === "'") {
      const end = value.indexOf(value[0], 1); // quoted: literal
      value = end !== -1 ? value.slice(1, end) : value.slice(1);
    } else {
      value = (' ' + value).split(/\s#/)[0].trim(); // trailing comment
    }
    vals[key] = value;
  }
  return vals;
}

// The real environment wins, as in the Python files. Lets .env hold PYTHON too,
// which matters on Windows.
function loadEnvFile(dir) {
  const merged = { ...process.env };
  for (const [key, value] of Object.entries(parseEnvFile(dir))) {
    if (!merged[key]) merged[key] = value;
  }
  return merged;
}

function selfStart(mod, apps) {
  if (require.main !== mod) return;

  const root = path.dirname(mod.filename);
  const dotenv = parseEnvFile(root);
  const env = Object.fromEntries(
    Object.entries(loadEnvFile(root))
      .filter(([k]) => PM2_KEEP.test(k) || !PM2_VARS.test(k))
  );
  // Windows needs the .cmd and, since Node 20, a shell to run it; with a shell
  // the args are joined verbatim, so anything with a space must be quoted.
  const PM2 = WIN ? "pm2.cmd" : "pm2";
  const quote = (a) => (WIN && /\s/.test(a) ? `"${a}"` : a);
  const run = (args) =>
    spawnSync(PM2, args.map(quote), { shell: WIN, encoding: "utf8", env });

  // absolute script paths: PM2 resolves relative ones against the config file,
  // which here lives in the temp dir. watch:false because the data dir lives
  // under cwd and must never trigger a restart. .env goes in explicitly: PM2
  // remembers the environment an app was first started with, so without this an
  // edited .env would lose to the stale copy for the life of the daemon.
  const config = {
    apps: apps.map((app) => ({
      ...app,
      script: path.join(app.cwd, app.script),
      watch: false,
      env: { ...dotenv, ...(app.env || {}) },
    })),
  };
  const tmp = path.join(os.tmpdir(), `oro-pm2-${process.pid}.json`);
  fs.writeFileSync(tmp, JSON.stringify(config, null, 2));

  for (const app of apps) {
    run(["delete", app.name]); // not there yet on a first start; failure is fine
  }
  const res = run(["start", tmp]);
  if (res.status !== 0) {
    console.error(`[pm2_start] \`${PM2} start\` failed` +
                  (res.status === null ? "" : ` (exit ${res.status})`));
    if (res.error) console.error(`[pm2_start] ${res.error.message}`);
    for (const out of [res.stdout, res.stderr]) {
      if (out && out.trim()) console.error(out.trim());
    }
    console.error(`[pm2_start] the generated config is still at ${tmp}\n` +
                  `[pm2_start] start it by hand with: ${PM2} start "${tmp}"`);
    process.exitCode = 1;
  } else {
    if (res.stdout && res.stdout.trim()) console.log(res.stdout.trim());
    fs.unlinkSync(tmp);
    console.log(`[pm2_start] started ${apps.map((a) => a.name).join(", ")}; ` +
                "run `pm2 save && pm2 startup` to keep them across reboots");
  }

  // Drop the wrapper app PM2 created for this file, or it restart-loops - also
  // after a failure, so the error above is printed once instead of forever.
  // Detached, because that delete also kills this very process. In fork mode
  // argv[1] is PM2's own container, so the app name comes from the env.
  const self = process.env.name ||
    path.basename(process.argv[1] || __filename).replace(/\.[cm]?js$/, "");
  spawn(PM2, [quote("delete"), quote(self)],
        { detached: true, stdio: "ignore", shell: WIN, env }).unref();
}

module.exports = { selfStart, loadEnvFile };
