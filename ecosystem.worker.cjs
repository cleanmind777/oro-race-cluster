// PM2 config for EVERY EXTRA VPS (Linux or Windows).
//   pm2 start ecosystem.worker.cjs && pm2 save && pm2 startup
// Settings live in .env next to this file: MAIN_URL (with the port) and
// WORKER_ID are the two that matter. This process listens on NO port; it only
// makes outbound calls to MAIN_URL and to api.oroagents.com:443.
const { loadEnvFile } = require("./pm2_start.cjs");

const ROOT = __dirname;
const env = loadEnvFile(ROOT);

const apps = [
  {
    name: "oro-worker",
    script: "oro_worker.py",
    interpreter: env.PYTHON || "python3",
    cwd: ROOT,
    autorestart: true,
    max_restarts: 50,
    restart_delay: 2000,
    env: {
      WRITE_LOCAL: "0", // an extra VPS uploads to main and keeps no copy
    },
  },
];

module.exports = { apps };
require("./pm2_start.cjs").selfStart(module, apps);
