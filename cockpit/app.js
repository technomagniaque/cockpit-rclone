/**
 * app.js — contrôleur principal de l'extension Cockpit Rclone & Google Drive.
 * Zéro HTML non échappé (construction DOM / textContent) et zéro script inline (CSP-safe).
 */
import { callCtl } from "./api.js";
import {
  COMMON_BACKENDS,
  SCHEDULE_PRESETS,
  computePercent,
  formatBytes,
  formatEta,
  formatSpeed,
  joinRclonePath,
  parentRclonePath,
  slugifyId,
} from "./profiles.js";

const state = {
  overview: null,
  timerHandle: null,
  aboutCache: {},
  oauthSession: null,
  oauthPollHandle: null,
};

const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "className") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2).toLowerCase(), v);
    else if (v !== null && v !== undefined) node.setAttribute(k, String(v));
  }
  for (const child of children) {
    if (child === null || child === undefined) continue;
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

function showBanner(kind, message) {
  const b = $("banner");
  b.className = `alert alert-${kind}`;
  $("banner-text").textContent = message;
  b.classList.remove("hidden");
}

function hideBanner() {
  $("banner").classList.add("hidden");
}

async function runAction(fn, okMessage = "") {
  try {
    const res = await fn();
    if (okMessage) showBanner("ok", okMessage);
    return res;
  } catch (err) {
    showBanner("error", err && err.message ? err.message : String(err));
    return null;
  }
}

// ------------------------------------------------------------------ onglets

function initTabs() {
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const target = btn.dataset.tab;
      document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b === btn));
      document.querySelectorAll(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${target}`));
    });
  });
}

function switchToTab(name) {
  const btn = document.querySelector(`.tab-btn[data-tab="${name}"]`);
  if (btn) btn.click();
}

// ------------------------------------------------------------------ rendu principal

function renderDashboard(ov) {
  const sys = ov.system || {};
  $("topbar-subtitle").textContent = sys.rclone_installed
    ? `${sys.rclone_version} • config : ${sys.config_path}`
    : `rclone non installé • config cible : ${sys.config_path}`;

  $("install-warning").classList.toggle("hidden", Boolean(sys.rclone_installed));
  $("fuse-warning").classList.toggle("hidden", Boolean(sys.fuse_allow_other));

  $("kpi-rclone").textContent = sys.rclone_installed ? (sys.rclone_version.split(" ").slice(0, 2).join(" ") || "Installé") : "Non installé";
  $("kpi-fuse").textContent = `FUSE3 : ${sys.fuse3_installed ? "OK" : "absent"} • user_allow_other : ${sys.fuse_allow_other ? "actif" : "inactif"}`;

  const oauthCount = (ov.oauth_detected || []).length;
  $("kpi-remotes").textContent = `${(ov.remotes || []).length} remote(s)`;
  $("kpi-oauth").textContent = oauthCount
    ? `${oauthCount} clé(s) OAuth Google détectée(s) (${ov.oauth_detected[0].source})`
    : "Aucune clé OAuth locale détectée";

  const activeJobs = (ov.jobs || []).filter((j) => j.live && j.live.running).length;
  const activeMounts = (ov.mounts || []).filter((m) => m.service && m.service.active === "active").length;
  $("kpi-active").textContent = `${activeJobs} transfert(s) • ${activeMounts} montage(s)`;
  $("kpi-config").textContent = sys.config_path || "";

  // Transferts live
  const liveBox = $("live-transfers");
  liveBox.replaceChildren();
  const runningJobs = (ov.jobs || []).filter((j) => j.live && j.live.running);
  if (!runningJobs.length) {
    liveBox.appendChild(el("p", { className: "muted", text: "Aucun transfert en cours." }));
  } else {
    for (const job of runningJobs) {
      const lv = job.live;
      const pct = computePercent(lv.bytes, lv.totalBytes);
      const bwInput = el("input", { type: "text", value: lv.bwlimit || "off", style: "width: 120px;" });
      const card = el("div", { className: "card", style: "background: var(--surface-alt); margin-bottom: 10px;" }, [
        el("div", { className: "card-header" }, [
          el("div", {}, [
            el("strong", { text: `${job.name} (${job.mode})` }),
            " ",
            el("span", { className: "badge", text: lv.dry_run ? "SIMULATION" : "ACTIF" }),
            lv.zfs_snapshot ? el("span", { className: "badge badge-ok", text: `ZFS: ${lv.zfs_snapshot}` }) : null,
          ]),
          el("div", { className: "btn-group" }, [
            bwInput,
            el("button", {
              type: "button",
              className: "btn btn-sm",
              text: "Appliquer débit",
              onClick: () => runAction(
                () => callCtl(["job-bwlimit"], { id: job.id, rate: bwInput.value }),
                `Limite de débit mise à jour pour ${job.id}`,
              ).then(() => refreshOverview()),
            }),
            el("button", {
              type: "button",
              className: "btn btn-sm btn-danger",
              text: "Arrêter",
              onClick: () => runAction(() => callCtl(["job-stop", job.id]), `Arrêt demandé pour ${job.id}`).then(() => refreshOverview()),
            }),
          ]),
        ]),
        el("div", { className: "progress" }, [
          el("div", { className: "progress-bar", style: `width: ${pct}%` }),
        ]),
        el("div", {
          className: "muted",
          style: "margin-top: 6px;",
          text: `${pct}% • ${formatBytes(lv.bytes)} / ${formatBytes(lv.totalBytes)} • ${formatSpeed(lv.speed)} • ETA : ${formatEta(lv.eta)} • Fichiers : ${lv.transfers}/${lv.totalTransfers} • Erreurs : ${lv.errors}`,
        }),
        lv.lastError ? el("div", { className: "muted", style: "color: var(--danger);", text: `Dernière erreur : ${lv.lastError}` }) : null,
      ]);
      if (lv.transferring && lv.transferring.length) {
        const list = el("div", { className: "mono", style: "margin-top: 8px; font-size: 12px;" });
        for (const f of lv.transferring.slice(0, 6)) {
          list.appendChild(el("div", {
            text: `→ ${f.name} (${f.percentage}% • ${formatSpeed(f.speed)} • ETA ${formatEta(f.eta)})`,
          }));
        }
        card.appendChild(list);
      }
      liveBox.appendChild(card);
    }
  }

  // ZFS datasets
  const zfsBox = $("zfs-list");
  zfsBox.replaceChildren();
  const ds = ov.zfs_datasets || [];
  const datalist = $("zfs-datalist");
  datalist.replaceChildren();
  for (const d of ds) {
    datalist.appendChild(el("option", { value: d.mountpoint }));
  }
  for (const r of (ov.remotes || [])) {
    datalist.appendChild(el("option", { value: `${r.name}:` }));
  }
  if (!ds.length) {
    zfsBox.appendChild(el("p", { className: "muted", text: "Aucun dataset ZFS détecté sur cette machine." }));
  } else {
    const tbl = el("table", {}, [
      el("thead", {}, [el("tr", {}, [el("th", { text: "Dataset" }), el("th", { text: "Utilisé" }), el("th", { text: "Dispo" }), el("th", { text: "Montage" })])]),
      el("tbody", {}, ds.map((d) => el("tr", {}, [
        el("td", { className: "mono", text: d.name }),
        el("td", { text: d.used }),
        el("td", { text: d.avail }),
        el("td", { className: "mono", text: d.mountpoint }),
      ]))),
    ]);
    zfsBox.appendChild(tbl);
  }

  // Dashboard remotes
  const drBox = $("dashboard-remotes");
  drBox.replaceChildren();
  if (!(ov.remotes || []).length) {
    drBox.appendChild(el("p", { className: "muted", text: "Aucun remote configuré. Ajoutez Google Drive dans l'onglet Remotes." }));
  } else {
    for (const r of ov.remotes) {
      const about = state.aboutCache[r.name];
      const info = about
        ? `Utilisé : ${formatBytes(about.used)} / ${formatBytes(about.total)} (Libre : ${formatBytes(about.free)}, Corbeille : ${formatBytes(about.trashed)})`
        : "Cliquez sur Quota pour interroger l'espace disponible.";
      drBox.appendChild(el("div", { className: "explorer-row" }, [
        el("div", {}, [
          el("strong", { text: `${r.name}:` }),
          " ",
          el("span", { className: "badge", text: r.type }),
          el("div", { className: "muted", text: info }),
        ]),
        el("button", {
          type: "button",
          className: "btn btn-sm",
          text: "Quota",
          onClick: async () => {
            const res = await runAction(() => callCtl(["remote-about", r.name]));
            if (res && res.about) {
              state.aboutCache[r.name] = res.about;
              renderDashboard(state.overview);
            }
          },
        }),
      ]));
    }
  }
}

function renderRemotes(ov) {
  const tbody = $("remotes-tbody");
  tbody.replaceChildren();
  const remotes = ov.remotes || [];
  if (!remotes.length) {
    tbody.appendChild(el("tr", {}, [el("td", { colspan: "4", className: "muted", text: "Aucun remote configuré." })]));
  }
  for (const r of remotes) {
    const badges = [];
    if (r.type === "drive") {
      badges.push(el("span", {
        className: r.has_own_client_id ? "badge badge-ok" : "badge badge-warn",
        text: r.has_own_client_id ? "Client ID OAuth perso OK" : "Client ID partagé (bridé)",
      }));
      badges.push(" ", el("span", {
        className: r.has_token ? "badge badge-ok" : "badge badge-danger",
        text: r.has_token ? "Connecté Google" : "Token manquant",
      }));
      if (r.options.team_drive) {
        badges.push(" ", el("span", { className: "badge", text: "Shared Drive" }));
      }
    } else if (r.type === "crypt" && r.options.remote) {
      badges.push(el("span", { className: "mono", text: `→ ${r.options.remote}` }));
    }
    tbody.appendChild(el("tr", {}, [
      el("td", { className: "mono", text: `${r.name}:` }),
      el("td", {}, [el("span", { className: "badge", text: r.type })]),
      el("td", {}, badges.length ? badges : [el("span", { className: "muted", text: `${Object.keys(r.options || {}).length} option(s)` })]),
      el("td", {}, [
        el("div", { className: "btn-group" }, [
          r.type === "drive"
            ? el("button", {
                type: "button",
                className: r.has_token ? "btn btn-sm" : "btn btn-sm btn-primary",
                text: r.has_token ? "Reconnecter Google" : "Connecter Google",
                onClick: () => {
                  populateRemoteForm(r);
                  startGoogleOAuthFlow(r.name);
                },
              })
            : null,
          el("button", { type: "button", className: "btn btn-sm", text: "Modifier", onClick: () => populateRemoteForm(r) }),
          el("button", {
            type: "button",
            className: "btn btn-sm",
            text: "Corbeille (cleanup)",
            onClick: () => {
              if (window.confirm(`Vider définitivement la corbeille de ${r.name}: ?`)) {
                runAction(() => callCtl(["remote-cleanup", r.name]), `Corbeille de ${r.name}: vidée.`);
              }
            },
          }),
          el("button", {
            type: "button",
            className: "btn btn-sm btn-danger",
            text: "Supprimer",
            onClick: () => {
              if (window.confirm(`Supprimer le remote ${r.name} de rclone.conf ?`)) {
                runAction(() => callCtl(["remote-delete", r.name]), `Remote ${r.name} supprimé.`).then(() => refreshOverview());
              }
            },
          }),
        ]),
      ]),
    ]));
  }

  // OAuth files dropdown
  const oauthSel = $("rem-oauth-file");
  const prevVal = oauthSel.value;
  oauthSel.replaceChildren(el("option", { value: "", text: "— Saisie manuelle ou JSON collé ci-dessous —" }));
  for (const f of (ov.oauth_detected || [])) {
    oauthSel.appendChild(el("option", {
      value: f.path,
      text: `${f.source} : ${f.path} (${f.client_id_preview})`,
    }));
  }
  if (prevVal) oauthSel.value = prevVal;
  else if ((ov.oauth_detected || []).length) oauthSel.value = ov.oauth_detected[0].path;

  // Explorer remote dropdown
  const expSel = $("exp-right-remote");
  const prevRem = expSel.value;
  expSel.replaceChildren(el("option", { value: "", text: "Remote…" }));
  for (const r of remotes) {
    expSel.appendChild(el("option", { value: `${r.name}:`, text: `${r.name}: (${r.type})` }));
  }
  if (prevRem) expSel.value = prevRem;
}

function populateRemoteForm(r) {
  $("remote-form-title").textContent = `Modifier le remote « ${r.name} »`;
  $("rem-name").value = r.name;
  $("rem-type").value = COMMON_BACKENDS.some((b) => b.id === r.type) ? r.type : "drive";
  toggleRemoteBoxes();
  const opts = r.options || {};
  if (r.type === "drive") {
    $("rem-gdrive-scope").value = opts.scope || "drive";
    $("rem-gdrive-team").value = opts.team_drive || "";
    $("rem-gdrive-root").value = opts.root_folder_id || "";
    $("rem-gdrive-cid").value = opts.client_id || "";
    $("rem-gdrive-csec").value = "";
    $("rem-gdrive-token").value = "";
  } else if (r.type === "crypt") {
    $("rem-crypt-remote").value = opts.remote || "";
    $("rem-crypt-fname").value = opts.filename_encryption || "standard";
    $("rem-crypt-pass").value = "";
    $("rem-crypt-pass2").value = "";
  }
  const known = new Set(["scope", "team_drive", "root_folder_id", "client_id", "client_secret", "token", "remote", "filename_encryption", "password", "password2"]);
  const extraLines = Object.entries(opts)
    .filter(([k]) => !known.has(k))
    .map(([k, v]) => `${k} = ${v}`);
  $("rem-extra").value = extraLines.join("\n");
}

function toggleRemoteBoxes() {
  const t = $("rem-type").value;
  $("rem-gdrive-box").classList.toggle("hidden", t !== "drive");
  $("rem-crypt-box").classList.toggle("hidden", t !== "crypt");
}

function renderJobs(ov) {
  const tbody = $("jobs-tbody");
  tbody.replaceChildren();
  const jobs = ov.jobs || [];
  if (!jobs.length) {
    tbody.appendChild(el("tr", {}, [el("td", { colspan: "6", className: "muted", text: "Aucune tâche configurée." })]));
  }
  for (const j of jobs) {
    const running = Boolean(j.live && j.live.running) || (j.service && j.service.active === "active");
    const statusBadge = running
      ? el("span", { className: "badge badge-ok", text: "En cours" })
      : el("span", { className: "badge", text: (j.service && j.service.sub) || "inactif" });
    const sched = j.schedule
      ? `${j.schedule} (${j.timer && j.timer.enabled === "enabled" ? "actif" : "inactif"})`
      : "Manuel";

    tbody.appendChild(el("tr", {}, [
      el("td", {}, [
        el("strong", { text: j.name || j.id }),
        el("div", { className: "muted mono", text: j.id }),
      ]),
      el("td", {}, [
        el("span", { className: "badge", text: j.mode }),
        " ",
        el("span", { className: "muted", text: j.profile || "custom" }),
        j.zfs_snapshot ? el("div", { className: "muted", text: "Snapshot ZFS atomique" }) : null,
      ]),
      el("td", { className: "mono", text: j.mode === "dedupe" ? j.source : `${j.source} → ${j.destination}` }),
      el("td", { className: "muted", text: sched }),
      el("td", {}, [statusBadge]),
      el("td", {}, [
        el("div", { className: "btn-group" }, [
          el("button", {
            type: "button",
            className: "btn btn-sm",
            text: "Simuler",
            onClick: () => runAction(() => callCtl(["job-start", j.id, "--dry-run"]), `Simulation lancée pour ${j.id}`).then(() => refreshOverview()),
          }),
          el("button", {
            type: "button",
            className: "btn btn-sm btn-primary",
            text: "Lancer",
            onClick: () => runAction(() => callCtl(["job-start", j.id]), `Tâche ${j.id} lancée`).then(() => refreshOverview()),
          }),
          running
            ? el("button", {
                type: "button",
                className: "btn btn-sm btn-danger",
                text: "Arrêter",
                onClick: () => runAction(() => callCtl(["job-stop", j.id]), `Arrêt de ${j.id}`).then(() => refreshOverview()),
              })
            : null,
          el("button", { type: "button", className: "btn btn-sm", text: "Timer", onClick: () => openTimerDialog(j) }),
          el("button", { type: "button", className: "btn btn-sm", text: "Logs", onClick: () => openLogs(`cockpit-rclone-job@${j.id}.service`) }),
          el("button", { type: "button", className: "btn btn-sm", text: "Éditer", onClick: () => populateJobForm(j) }),
          el("button", {
            type: "button",
            className: "btn btn-sm btn-danger",
            text: "Suppr.",
            onClick: () => {
              if (window.confirm(`Supprimer la tâche ${j.id} et son éventuel timer ?`)) {
                runAction(() => callCtl(["job-delete", j.id]), `Tâche ${j.id} supprimée`).then(() => refreshOverview());
              }
            },
          }),
        ]),
      ]),
    ]));
  }
}

function populateJobForm(j) {
  $("job-form-title").textContent = `Modifier la tâche « ${j.id} »`;
  $("job-id").value = j.id;
  $("job-name").value = j.name || "";
  $("job-mode").value = j.mode || "copy";
  $("job-profile").value = j.profile || "gdrive-nas-heavy";
  updateJobProfileDesc();
  $("job-source").value = j.source || "";
  $("job-dest").value = j.destination || "";
  const o = j.options || {};
  $("job-bwlimit").value = o.bwlimit || "";
  $("job-backup-dir").value = o.backup_dir || "";
  $("job-transfers").value = o.transfers || "";
  $("job-checkers").value = o.checkers || "";
  $("job-chunk").value = o.drive_chunk_size || "";
  $("job-dedupe-mode").value = o.dedupe_mode || "rename";
  $("job-zfs-snap").checked = Boolean(j.zfs_snapshot);
  $("job-checksum").checked = Boolean(o.checksum);
  $("job-update").checked = Boolean(o.update);
  $("job-skip-gdocs").checked = Boolean(o.drive_skip_gdocs);
  $("job-excludes").value = (j.excludes || []).join("\n");
  $("job-includes").value = (j.includes || []).join("\n");
}

function renderMounts(ov) {
  const tbody = $("mounts-tbody");
  tbody.replaceChildren();
  const mounts = ov.mounts || [];
  if (!mounts.length) {
    tbody.appendChild(el("tr", {}, [el("td", { colspan: "6", className: "muted", text: "Aucun montage FUSE configuré." })]));
  }
  for (const m of mounts) {
    const active = m.service && m.service.active === "active";
    const enabled = m.service && m.service.enabled === "enabled";
    tbody.appendChild(el("tr", {}, [
      el("td", { className: "mono", text: m.id }),
      el("td", { className: "muted", text: m.profile || "custom" }),
      el("td", { className: "mono", text: `${m.remote_path} → ${m.mountpoint}` }),
      el("td", { className: "muted", text: `uid=${m.uid ?? "root"}:${m.gid ?? "root"} • ${m.vfs_cache_max_size || "profil"}` }),
      el("td", {}, [
        el("span", { className: active ? "badge badge-ok" : "badge", text: active ? "Monté" : "Arrêté" }),
        " ",
        el("span", { className: "badge", text: enabled ? "auto-boot" : "manuel" }),
      ]),
      el("td", {}, [
        el("div", { className: "btn-group" }, [
          el("button", {
            type: "button",
            className: "btn btn-sm btn-primary",
            text: active ? "Redémarrer" : "Monter",
            onClick: () => runAction(() => callCtl(["mount-action", m.id, active ? "restart" : "start"]), `Montage ${m.id} lancé`).then(() => refreshOverview()),
          }),
          active
            ? el("button", {
                type: "button",
                className: "btn btn-sm",
                text: "Démonter",
                onClick: () => runAction(() => callCtl(["mount-action", m.id, "stop"]), `Montage ${m.id} démonté`).then(() => refreshOverview()),
              })
            : null,
          el("button", {
            type: "button",
            className: "btn btn-sm",
            text: enabled ? "Désactiver boot" : "Activer boot",
            onClick: () => runAction(() => callCtl(["mount-action", m.id, enabled ? "disable" : "enable"]), `Boot mis à jour pour ${m.id}`).then(() => refreshOverview()),
          }),
          el("button", { type: "button", className: "btn btn-sm", text: "Logs", onClick: () => openLogs(`cockpit-rclone-mount@${m.id}.service`) }),
          el("button", {
            type: "button",
            className: "btn btn-sm btn-danger",
            text: "Suppr.",
            onClick: () => {
              if (window.confirm(`Supprimer le montage ${m.id} ?`)) {
                runAction(() => callCtl(["mount-delete", m.id]), `Montage ${m.id} supprimé`).then(() => refreshOverview());
              }
            },
          }),
        ]),
      ]),
    ]));
  }

  // Utilisateurs locaux pour UID:GID
  const userSel = $("mnt-user");
  const prev = userSel.value;
  userSel.replaceChildren(el("option", { value: "", text: "root (0:0)" }));
  for (const u of ((ov.system && ov.system.users) || [])) {
    if (u.uid === 0) continue;
    userSel.appendChild(el("option", { value: `${u.uid}:${u.gid}`, text: `${u.name} (${u.uid}:${u.gid})` }));
  }
  if (prev) userSel.value = prev;
  else if (userSel.options.length > 1) userSel.selectedIndex = 1;
}

function renderServesAndMaintenance(ov) {
  const tbody = $("serves-tbody");
  tbody.replaceChildren();
  const serves = ov.serves || [];
  if (!serves.length) {
    tbody.appendChild(el("tr", {}, [el("td", { colspan: "5", className: "muted", text: "Aucun serveur configuré." })]));
  }
  for (const s of serves) {
    const active = s.service && s.service.active === "active";
    tbody.appendChild(el("tr", {}, [
      el("td", { className: "mono", text: s.id }),
      el("td", {}, [el("span", { className: "badge", text: s.protocol })]),
      el("td", { className: "mono", text: `${s.remote_path} @ ${s.addr}` }),
      el("td", {}, [el("span", { className: active ? "badge badge-ok" : "badge", text: active ? "Actif" : "Arrêté" })]),
      el("td", {}, [
        el("div", { className: "btn-group" }, [
          el("button", {
            type: "button",
            className: "btn btn-sm btn-primary",
            text: active ? "Arrêter" : "Démarrer",
            onClick: () => runAction(() => callCtl(["serve-action", s.id, active ? "stop" : "start"])).then(() => refreshOverview()),
          }),
          el("button", { type: "button", className: "btn btn-sm", text: "Logs", onClick: () => openLogs(`cockpit-rclone-serve@${s.id}.service`) }),
          el("button", {
            type: "button",
            className: "btn btn-sm btn-danger",
            text: "Suppr.",
            onClick: () => runAction(() => callCtl(["serve-delete", s.id])).then(() => refreshOverview()),
          }),
        ]),
      ]),
    ]));
  }

  const snaps = (ov.system && ov.system.orphan_snapshots) || [];
  const snapBox = $("orphan-snaps");
  snapBox.replaceChildren();
  if (!snaps.length) {
    snapBox.appendChild(el("span", { className: "badge badge-ok", text: "Aucun snapshot orphelin" }));
  } else {
    for (const sn of snaps) {
      snapBox.appendChild(el("div", { className: "mono", text: sn }));
    }
  }
}

// ------------------------------------------------------------------ explorateur

async function loadExplorerPane(side) {
  const input = $(`exp-${side}-path`);
  const list = $(`exp-${side}-list`);
  const meta = $(`exp-${side}-meta`);
  const target = input.value.trim();
  if (!target) return;
  meta.textContent = "Chargement…";
  const res = await runAction(() => callCtl(["browse"], { target }));
  if (!res) {
    meta.textContent = "Erreur de lecture.";
    return;
  }
  const items = res.items || [];
  meta.textContent = `${items.length} élément(s) dans ${res.target}`;
  list.replaceChildren();
  if (!items.length) {
    list.appendChild(el("div", { className: "explorer-row muted", text: "(dossier vide)" }));
    return;
  }
  for (const item of items) {
    const full = joinRclonePath(target, item.Name);
    const nameEl = el("span", {
      className: "explorer-name",
      text: `${item.IsDir ? "📁 " : "📄 "}${item.Name}`,
      onClick: () => {
        if (item.IsDir) {
          input.value = full;
          loadExplorerPane(side);
        }
      },
    });
    list.appendChild(el("div", { className: "explorer-row" }, [
      nameEl,
      el("div", { className: "btn-group" }, [
        el("span", { className: "muted", text: item.IsDir ? "dossier" : formatBytes(item.Size) }),
        el("button", {
          type: "button",
          className: "btn btn-sm btn-danger",
          text: "×",
          title: "Supprimer",
          onClick: () => {
            if (window.confirm(`Supprimer ${item.IsDir ? "le dossier et son contenu" : "le fichier"} : ${full} ?`)) {
              runAction(() => callCtl(["delete-path"], { target: full, is_dir: Boolean(item.IsDir) }), `Supprimé : ${full}`)
                .then(() => loadExplorerPane(side));
            }
          },
        }),
      ]),
    ]));
  }
}

// ------------------------------------------------------------------ dialogues

async function openLogs(unit) {
  $("dlg-logs-title").textContent = `Journaux — ${unit}`;
  $("dlg-logs-content").textContent = "Chargement…";
  $("dlg-logs").showModal();
  const res = await runAction(() => callCtl(["logs", unit, "200"]));
  $("dlg-logs-content").textContent = (res && res.logs) || "(aucun log)";
}

function openTimerDialog(job) {
  $("dlg-timer-title").textContent = `Planification — ${job.name || job.id}`;
  $("tmr-job-id").value = job.id;
  $("tmr-calendar").value = job.schedule || "*-*-* 01:00:00";
  $("tmr-delay").value = job.randomized_delay || "5m";
  $("tmr-enabled").checked = !job.timer || job.timer.enabled === "enabled";
  $("dlg-timer").showModal();
}

// ------------------------------------------------------------------ profils & formulaires

function populateProfileSelects(ov) {
  const jobSel = $("job-profile");
  if (!jobSel.options.length && ov.profiles) {
    for (const [id, info] of Object.entries(ov.profiles)) {
      jobSel.appendChild(el("option", { value: id, text: info.label }));
    }
    jobSel.value = "gdrive-nas-heavy";
    updateJobProfileDesc();
  }
  const mntSel = $("mnt-profile");
  if (!mntSel.options.length && ov.mount_profiles) {
    for (const [id, info] of Object.entries(ov.mount_profiles)) {
      mntSel.appendChild(el("option", { value: id, text: info.label }));
    }
    mntSel.value = "gdrive-z640-desktop";
    updateMountProfileDesc();
  }
}

function updateJobProfileDesc() {
  const profiles = (state.overview && state.overview.profiles) || {};
  const info = profiles[$("job-profile").value];
  $("job-profile-desc").textContent = info ? info.description : "";
}

function updateMountProfileDesc() {
  const profiles = (state.overview && state.overview.mount_profiles) || {};
  const info = profiles[$("mnt-profile").value];
  $("mnt-profile-desc").textContent = info ? info.description : "";
}

function stopOAuthPolling() {
  if (state.oauthPollHandle) {
    clearInterval(state.oauthPollHandle);
    state.oauthPollHandle = null;
  }
}

function closeOAuthFlowBox() {
  stopOAuthPolling();
  state.oauthSession = null;
  $("oauth-flow-box").classList.add("hidden");
  $("oauth-callback-input").value = "";
}

async function startGoogleOAuthFlow(overrideRemoteName = "") {
  const name = (overrideRemoteName || $("rem-name").value || "").trim();
  if (!name) {
    showBanner("warn", "Indiquez d'abord le nom du remote (ex. t1 ou gdrive) avant de lancer la connexion Google.");
    $("rem-name").focus();
    return;
  }
  $("rem-name").value = name;
  $("rem-type").value = "drive";
  toggleRemoteBoxes();

  // Ouvre l'onglet synchroniquement dans le geste utilisateur pour éviter le blocage popup
  const popup = window.open("about:blank", "_blank");
  const payload = {
    name,
    scope: $("rem-gdrive-scope").value || "drive",
    team_drive: $("rem-gdrive-team").value.trim(),
    root_folder_id: $("rem-gdrive-root").value.trim(),
    client_id: $("rem-gdrive-cid").value.trim(),
    client_secret: $("rem-gdrive-csec").value.trim(),
    use_oauth_file: $("rem-oauth-file").value,
    oauth_json: $("rem-oauth-json").value.trim(),
  };

  const res = await runAction(() => callCtl(["gdrive-oauth-start"], payload));
  if (!res || !res.auth_url) {
    if (popup && !popup.closed) popup.close();
    return;
  }

  state.oauthSession = { session_id: res.session_id, remote: res.remote };
  $("oauth-direct-link").setAttribute("href", res.auth_url);
  $("oauth-flow-status").textContent =
    `Connexion Google ouverte pour « ${res.remote}: » (client ${res.client_id_preview}). ` +
    "Validez l'accès dans l'onglet Google…";
  $("oauth-flow-box").className = "alert alert-ok";
  $("oauth-flow-box").classList.remove("hidden");
  $("oauth-callback-input").value = "";

  if (popup && !popup.closed) {
    popup.location.href = res.auth_url;
  } else {
    showBanner("warn", "Votre navigateur a bloqué l'ouverture automatique : cliquez sur « Rouvrir la page de connexion Google » ci-dessous.");
  }

  stopOAuthPolling();
  state.oauthPollHandle = setInterval(async () => {
    if (!state.oauthSession) {
      stopOAuthPolling();
      return;
    }
    try {
      const st = await callCtl(["gdrive-oauth-status", state.oauthSession.session_id]);
      if (st.status === "completed") {
        const rname = state.oauthSession.remote;
        closeOAuthFlowBox();
        showBanner("ok", `Remote « ${rname}: » connecté à Google Drive ! Le token OAuth a été enregistré dans rclone.conf.`);
        await refreshOverview();
      } else if (st.status === "error" || st.status === "expired") {
        stopOAuthPolling();
        $("oauth-flow-box").className = "alert alert-error";
        $("oauth-flow-status").textContent = st.error || "Session OAuth expirée.";
      }
    } catch {
      // ignore transient poll error
    }
  }, 2000);
}

async function finishGoogleOAuthFlow() {
  if (!state.oauthSession) {
    showBanner("warn", "Lancez d'abord « Ouvrir la vraie page de connexion Google ».");
    return;
  }
  const callbackInput = $("oauth-callback-input").value.trim();
  if (!callbackInput) {
    showBanner("warn", "Collez l'URL http://127.0.0.1:53682/?state=...&code=... (ou le code seul).");
    return;
  }
  const res = await runAction(
    () => callCtl(["gdrive-oauth-finish"], {
      session_id: state.oauthSession.session_id,
      callback_input: callbackInput,
    }),
    `Remote « ${state.oauthSession.remote}: » connecté à Google Drive !`,
  );
  if (res && res.ok) {
    closeOAuthFlowBox();
    await refreshOverview();
  }
}

function bindEvents() {
  $("banner-close").addEventListener("click", hideBanner);
  $("btn-refresh").addEventListener("click", () => refreshOverview());

  // Remotes
  for (const b of COMMON_BACKENDS) {
    $("rem-type").appendChild(el("option", { value: b.id, text: b.label }));
  }
  $("rem-type").addEventListener("change", toggleRemoteBoxes);
  $("btn-new-remote").addEventListener("click", () => {
    $("remote-form").reset();
    $("remote-form-title").textContent = "Nouveau remote";
    closeOAuthFlowBox();
    toggleRemoteBoxes();
  });
  $("btn-reset-remote").addEventListener("click", () => {
    $("remote-form").reset();
    closeOAuthFlowBox();
  });

  $("btn-gdrive-oauth-login").addEventListener("click", () => startGoogleOAuthFlow());
  $("btn-gdrive-oauth-finish").addEventListener("click", () => finishGoogleOAuthFlow());
  $("btn-gdrive-oauth-cancel").addEventListener("click", closeOAuthFlowBox);

  $("btn-auth-hint").addEventListener("click", async () => {
    const res = await runAction(() => callCtl(["gdrive-auth-hint"], { oauth_path: $("rem-oauth-file").value }));
    if (res && res.command) {
      $("auth-hint-box").textContent = res.command;
      $("auth-hint-box").classList.remove("hidden");
    }
  });

  $("remote-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const type = $("rem-type").value;
    const options = {};
    for (const line of $("rem-extra").value.split("\n")) {
      const idx = line.indexOf("=");
      if (idx > 0) options[line.slice(0, idx).trim()] = line.slice(idx + 1).trim();
    }
    const payload = { name: $("rem-name").value.trim(), type, options };
    if (type === "drive") {
      options.scope = $("rem-gdrive-scope").value;
      if ($("rem-gdrive-team").value.trim()) options.team_drive = $("rem-gdrive-team").value.trim();
      if ($("rem-gdrive-root").value.trim()) options.root_folder_id = $("rem-gdrive-root").value.trim();
      if ($("rem-gdrive-cid").value.trim()) options.client_id = $("rem-gdrive-cid").value.trim();
      if ($("rem-gdrive-csec").value.trim()) options.client_secret = $("rem-gdrive-csec").value.trim();
      if ($("rem-gdrive-token").value.trim()) options.token = $("rem-gdrive-token").value.trim();
      if ($("rem-oauth-file").value) payload.use_oauth_file = $("rem-oauth-file").value;
      if ($("rem-oauth-json").value.trim()) payload.oauth_json = $("rem-oauth-json").value.trim();
    } else if (type === "crypt") {
      options.remote = $("rem-crypt-remote").value.trim();
      options.filename_encryption = $("rem-crypt-fname").value;
      if ($("rem-crypt-pass").value) options.password = $("rem-crypt-pass").value;
      if ($("rem-crypt-pass2").value) options.password2 = $("rem-crypt-pass2").value;
    }
    const res = await runAction(() => callCtl(["remote-save"], payload), `Remote « ${payload.name} » enregistré.`);
    if (res) {
      $("rem-gdrive-token").value = "";
      $("rem-gdrive-csec").value = "";
      $("rem-oauth-json").value = "";
      await refreshOverview();
    }
  });

  // Jobs
  $("job-profile").addEventListener("change", updateJobProfileDesc);
  $("btn-new-job").addEventListener("click", () => {
    $("job-form").reset();
    $("job-form-title").textContent = "Nouvelle tâche";
    $("job-zfs-snap").checked = true;
    updateJobProfileDesc();
  });
  $("btn-reset-job").addEventListener("click", () => $("job-form").reset());

  $("job-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const id = slugifyId($("job-id").value || $("job-name").value);
    const options = {};
    if ($("job-bwlimit").value.trim()) options.bwlimit = $("job-bwlimit").value.trim();
    if ($("job-backup-dir").value.trim()) options.backup_dir = $("job-backup-dir").value.trim();
    if ($("job-transfers").value) options.transfers = Number($("job-transfers").value);
    if ($("job-checkers").value) options.checkers = Number($("job-checkers").value);
    if ($("job-chunk").value.trim()) options.drive_chunk_size = $("job-chunk").value.trim();
    if ($("job-mode").value === "dedupe") options.dedupe_mode = $("job-dedupe-mode").value;
    if ($("job-checksum").checked) options.checksum = true;
    if ($("job-update").checked) options.update = true;
    if ($("job-skip-gdocs").checked) options.drive_skip_gdocs = true;

    const payload = {
      id,
      name: $("job-name").value.trim() || id,
      mode: $("job-mode").value,
      profile: $("job-profile").value,
      source: $("job-source").value.trim(),
      destination: $("job-dest").value.trim(),
      zfs_snapshot: $("job-zfs-snap").checked,
      excludes: $("job-excludes").value.split("\n").map((s) => s.trim()).filter(Boolean),
      includes: $("job-includes").value.split("\n").map((s) => s.trim()).filter(Boolean),
      options,
    };
    const res = await runAction(() => callCtl(["job-save"], payload), `Tâche « ${id} » enregistrée.`);
    if (res) await refreshOverview();
  });

  // Explorateur
  for (const side of ["left", "right"]) {
    $(`exp-${side}-go`).addEventListener("click", () => loadExplorerPane(side));
    $(`exp-${side}-up`).addEventListener("click", () => {
      $(`exp-${side}-path`).value = parentRclonePath($(`exp-${side}-path`).value);
      loadExplorerPane(side);
    });
    $(`exp-${side}-size`).addEventListener("click", async () => {
      const target = $(`exp-${side}-path`).value.trim();
      const res = await runAction(() => callCtl(["size"], { target }));
      if (res && res.size) {
        $(`exp-${side}-meta`).textContent = `${target} : ${res.size.count} fichier(s), ${formatBytes(res.size.bytes)}`;
      }
    });
    $(`exp-${side}-mkdir`).addEventListener("click", async () => {
      const name = window.prompt("Nom du nouveau dossier :");
      if (!name) return;
      const target = joinRclonePath($(`exp-${side}-path`).value.trim(), name);
      const res = await runAction(() => callCtl(["mkdir"], { target }), `Dossier créé : ${target}`);
      if (res) loadExplorerPane(side);
    });
  }
  $("exp-right-remote").addEventListener("change", () => {
    if ($("exp-right-remote").value) {
      $("exp-right-path").value = $("exp-right-remote").value;
      loadExplorerPane("right");
    }
  });
  $("btn-explorer-to-job").addEventListener("click", () => {
    const src = $("exp-left-path").value.trim();
    const dst = $("exp-right-path").value.trim();
    $("job-source").value = src;
    $("job-dest").value = dst;
    $("job-id").value = slugifyId(`${src.split("/").pop() || "nas"}-vers-${dst.split(":")[0] || "cloud"}`);
    switchToTab("jobs");
  });

  // Montages FUSE
  $("mnt-profile").addEventListener("change", updateMountProfileDesc);
  $("mount-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const [uidStr, gidStr] = ($("mnt-user").value || "").split(":");
    const payload = {
      id: slugifyId($("mnt-id").value),
      profile: $("mnt-profile").value,
      remote_path: $("mnt-remote").value.trim(),
      mountpoint: $("mnt-point").value.trim(),
      cache_dir: $("mnt-cache-dir").value.trim(),
      vfs_cache_max_size: $("mnt-cache-size").value.trim(),
      bwlimit: $("mnt-bwlimit").value.trim(),
      uid: uidStr ? Number(uidStr) : null,
      gid: gidStr ? Number(gidStr) : null,
      allow_other: $("mnt-allow-other").checked,
      read_only: $("mnt-ro").checked,
    };
    const res = await runAction(() => callCtl(["mount-save"], payload), `Montage « ${payload.id} » enregistré.`);
    if (res) await refreshOverview();
  });

  // Serve & maintenance
  $("serve-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = {
      id: slugifyId($("srv-id").value),
      protocol: $("srv-proto").value,
      remote_path: $("srv-remote").value.trim(),
      addr: $("srv-addr").value.trim(),
      user: $("srv-user").value.trim(),
      pass: $("srv-pass").value,
      read_only: $("srv-ro").checked,
      run_as: $("srv-run-as").value.trim() || "cockpit-rclone",
      dlna_lan_ack: $("srv-dlna-ack").checked,
    };
    const res = await runAction(() => callCtl(["serve-save"], payload), `Serveur « ${payload.id} » enregistré.`);
    if (res) {
      $("srv-pass").value = "";
      await refreshOverview();
    }
  });
  $("btn-clean-snaps").addEventListener("click", () => {
    runAction(() => callCtl(["zfs-snapshot-cleanup"]), "Nettoyage des snapshots ZFS effectué.").then(() => refreshOverview());
  });

  // Dialogues
  $("dlg-logs-close").addEventListener("click", () => $("dlg-logs").close());
  $("dlg-timer-close").addEventListener("click", () => $("dlg-timer").close());
  $("tmr-preset").appendChild(el("option", { value: "", text: "— Choisir un préréglage —" }));
  for (const p of SCHEDULE_PRESETS) {
    $("tmr-preset").appendChild(el("option", { value: p.calendar, text: p.label }));
  }
  $("tmr-preset").addEventListener("change", () => {
    if ($("tmr-preset").value) $("tmr-calendar").value = $("tmr-preset").value;
  });
  $("timer-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const payload = {
      id: $("tmr-job-id").value,
      schedule: $("tmr-calendar").value.trim(),
      randomized_delay: $("tmr-delay").value.trim() || "5m",
      enabled: $("tmr-enabled").checked,
    };
    const res = await runAction(() => callCtl(["timer-set"], payload), `Timer enregistré pour ${payload.id}.`);
    if (res) {
      $("dlg-timer").close();
      await refreshOverview();
    }
  });
  $("btn-timer-remove").addEventListener("click", async () => {
    const id = $("tmr-job-id").value;
    const res = await runAction(() => callCtl(["timer-remove", id]), `Timer supprimé pour ${id}.`);
    if (res) {
      $("dlg-timer").close();
      await refreshOverview();
    }
  });
}

async function refreshOverview() {
  const ov = await runAction(() => callCtl(["overview"]));
  if (!ov) return;
  state.overview = ov;
  populateProfileSelects(ov);
  renderDashboard(ov);
  renderRemotes(ov);
  renderJobs(ov);
  renderMounts(ov);
  renderServesAndMaintenance(ov);
}

function startPolling() {
  if (state.timerHandle) clearInterval(state.timerHandle);
  state.timerHandle = setInterval(() => {
    if ($("auto-refresh").checked && document.visibilityState === "visible") {
      refreshOverview();
    }
  }, 4000);
}

document.addEventListener("DOMContentLoaded", () => {
  initTabs();
  bindEvents();
  toggleRemoteBoxes();
  refreshOverview();
  startPolling();
});
