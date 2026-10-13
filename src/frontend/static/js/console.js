/* Shared helpers for the UHI Cloud Console */
const UHI = (() => {
  async function api(path, opts = {}) {
    const init = { method: opts.method || "GET", headers: {}, credentials: "same-origin" };
    if (opts.body instanceof FormData) init.body = opts.body;
    else if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); init.headers["Content-Type"] = "application/json"; }
    const res = await fetch("/api/v1" + path, init);
    let data = null;
    const ct = res.headers.get("content-type") || "";
    data = ct.includes("json") ? await res.json() : await res.text();
    if (!res.ok) {
      const msg = (data && data.message) || res.statusText;
      if (!opts.quiet) toast(`${res.status}: ${msg}`, true);
      const err = new Error(msg); err.status = res.status; throw err;
    }
    return data;
  }

  function toast(msg, isErr = false) {
    let box = document.getElementById("toasts");
    const t = document.createElement("div");
    t.className = "toast" + (isErr ? " err" : "");
    t.textContent = msg;
    box.appendChild(t);
    setTimeout(() => t.remove(), isErr ? 6500 : 3800);
  }

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const num = (v, d = 2) => (v === null || v === undefined || isNaN(v)) ? "-" : Number(v).toFixed(d);
  const bytes = (b) => { if (!b && b !== 0) return "-"; const u = ["B", "KB", "MB", "GB"]; let i = 0; while (b >= 1024 && i < 3) { b /= 1024; i++; } return b.toFixed(i ? 1 : 0) + " " + u[i]; };
  const ago = (iso) => {
    if (!iso || iso === "-") return "-";
    const s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (s < 60) return Math.max(1, Math.round(s)) + "s ago";
    if (s < 3600) return Math.round(s / 60) + "m ago";
    if (s < 86400) return Math.round(s / 3600) + "h ago";
    return new Date(iso).toLocaleDateString();
  };
  const local = (iso) => iso ? new Date(iso).toLocaleString() : "-";

  function badge(status) {
    const s = String(status ?? "-");
    const map = {
      ok: ["SUCCEEDED", "Completed", "InService", "Approved", "Available", "Active", "Deployed", "OK", "CONFIRMED", "DELIVERED", "Idle", "true", "Valid"],
      info: ["RUNNING", "InProgress", "Creating", "Running", "Starting", "Training", "Downloading", "Uploading", "PendingManualApproval", "PENDING"],
      bad: ["FAILED", "Failed", "ALARM", "Rejected", "DISABLED", "ABORTED", "Stopped", "STOPPED", "false"],
      warn: ["NoEndpoint", "INSUFFICIENT_DATA", "SKIPPED", "NotDeployed"],
    };
    let cls = "neutral";
    for (const [k, list] of Object.entries(map)) if (list.includes(s)) cls = k;
    return `<span class="badge ${cls}">${esc(s)}</span>`;
  }
  const risk = (r) => `<span class="badge risk-${esc(r)}">${esc(r)}</span>`;

  // UHI colour ramp (diverging: cool blue -> neutral -> hot red)
  function uhiColor(v) {
    const stops = [[-5, [33, 102, 172]], [-2, [103, 169, 207]], [0, [209, 229, 240]], [1.5, [254, 224, 144]],
                   [3, [253, 174, 97]], [5, [244, 109, 67]], [7, [215, 48, 39]], [10, [165, 0, 38]]];
    if (v <= stops[0][0]) return `rgb(${stops[0][1]})`;
    for (let i = 1; i < stops.length; i++) {
      if (v <= stops[i][0]) {
        const [a, ca] = stops[i - 1], [b, cb] = stops[i], t = (v - a) / (b - a);
        return `rgb(${ca.map((c, j) => Math.round(c + t * (cb[j] - c))).join(",")})`;
      }
    }
    return `rgb(${stops[stops.length - 1][1]})`;
  }
  function seqColor(v, min, max, cool = false) {
    const t = Math.max(0, Math.min(1, (v - min) / (max - min || 1)));
    const ramp = cool ? [[247, 252, 245], [116, 196, 118], [0, 90, 50]] : [[255, 255, 204], [253, 141, 60], [128, 0, 38]];
    const [a, b] = t < .5 ? [ramp[0], ramp[1]] : [ramp[1], ramp[2]], u = t < .5 ? t * 2 : (t - .5) * 2;
    return `rgb(${a.map((c, i) => Math.round(c + u * (b[i] - c))).join(",")})`;
  }

  // Chart.js defaults
  const charts = {};
  function chart(id, config) {
    if (charts[id]) charts[id].destroy();
    const el = document.getElementById(id);
    if (!el || typeof Chart === "undefined") return null;
    const dark = document.documentElement.dataset.theme === "dark";
    Chart.defaults.color = dark ? "#93a1b5" : "#5f6b7a";
    Chart.defaults.borderColor = dark ? "#263245" : "#e8ebf0";
    Chart.defaults.font.family = "Inter, system-ui, sans-serif";
    config.options = Object.assign({ responsive: true, maintainAspectRatio: false, plugins: { legend: { labels: { boxWidth: 12 } } } }, config.options || {});
    charts[id] = new Chart(el, config);
    return charts[id];
  }
  const PALETTE = ["#e8590c", "#0b63ce", "#1d8102", "#8e44ad", "#c4291c", "#00838f", "#b86e00"];

  function modal(title, html) {
    const bg = document.getElementById("modal");
    bg.querySelector(".modal-title").textContent = title;
    bg.querySelector(".modal-body").innerHTML = html;
    bg.classList.add("open");
  }
  function closeModal() { document.getElementById("modal").classList.remove("open"); }

  function tabs(root = document) {
    root.querySelectorAll("[data-tabs]").forEach((wrap) => {
      wrap.querySelectorAll(".tab").forEach((btn) => btn.addEventListener("click", () => {
        wrap.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b === btn));
        const scope = document.getElementById(wrap.dataset.tabs);
        scope.querySelectorAll(":scope > .tab-panel").forEach((p) => p.classList.toggle("active", p.id === btn.dataset.tab));
        btn.dispatchEvent(new CustomEvent("tabshown", { bubbles: true }));
      }));
    });
  }

  function poll(fn, ms) { fn(); return setInterval(fn, ms); }

  return { api, toast, esc, num, bytes, ago, local, badge, risk, uhiColor, seqColor, chart, PALETTE, modal, closeModal, tabs, poll };
})();

document.addEventListener("DOMContentLoaded", () => {
  UHI.tabs();
  document.getElementById("modal")?.addEventListener("click", (e) => { if (e.target.id === "modal" || e.target.closest("[data-close]")) UHI.closeModal(); });
  const tbtn = document.getElementById("themeToggle");
  tbtn?.addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("uhi-theme", next); } catch (e) {}
    window.dispatchEvent(new Event("themechange"));
  });
});
