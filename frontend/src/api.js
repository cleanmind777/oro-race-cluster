const TOKEN_KEY = "oro_cluster_token";

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || "";
}

export function setToken(value) {
  localStorage.setItem(TOKEN_KEY, value || "");
}

function headers(extra) {
  const h = { ...extra };
  const token = getToken();
  if (token) h["X-Cluster-Token"] = token;
  return h;
}

export async function getStatus() {
  const res = await fetch("/api/status", { headers: headers() });
  if (!res.ok) throw new Error(`status ${res.status}`);
  return res.json();
}

export async function startRace(raceId, label) {
  const res = await fetch("/api/races", {
    method: "POST",
    headers: headers({ "Content-Type": "application/json" }),
    body: JSON.stringify({ race_id: raceId.trim(), label: label.trim() }),
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}
