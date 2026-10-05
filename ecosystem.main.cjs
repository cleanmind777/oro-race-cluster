// PM2 config for the MAIN VPS: the API/UI server plus main's own local worker.
//   pm2 start ecosystem.main.cjs && pm2 save && pm2 startup
// All settings live in .env next to this file (see .env.example). Both Python
// programs read .env themselves, so nothing is duplicated here.
const { loadEnvFile } = require("./pm2_start.cjs");

const ROOT = __dirname;
const env = loadEnvFile(ROOT);
const PYTHON = env.PYTHON || (process.platform === "win32" ? "python" : "python3");

const apps = [
  {
    name: "oro-main",
    script: "oro_main.py",
    args: "serve",
    interpreter: PYTHON,
    cwd: ROOT,
    autorestart: true,
    max_restarts: 50,
    restart_delay: 2000,
    env: {},
  },
  {
    name: "oro-worker",
    script: "oro_worker.py",
    interpreter: PYTHON,
    cwd: ROOT,
    autorestart: true,
    max_restarts: 50,
    restart_delay: 2000,
    env: {
      // the only two things that make this worker main's own: it writes
      // straight to DATA_DIR instead of uploading, and it owns the "local" row
      WRITE_LOCAL: "1",
      WORKER_ID: env.WORKER_ID || "local",
    },
  },
];

module.exports = { apps };
require("./pm2_start.cjs").selfStart(module, apps);
