/**
 * profiles.js — métadonnées de profils, helpers purs de formatage et validation UI
 * (module ES pur, testable directement avec `node --test`).
 */

export const SCHEDULE_PRESETS = [
  { id: "daily-01", label: "Quotidien 01h00 (après reset quota GDrive)", calendar: "*-*-* 01:00:00" },
  { id: "every-6h", label: "Toutes les 6 heures", calendar: "*-*-* 00/6:00:00" },
  { id: "hourly", label: "Toutes les heures", calendar: "hourly" },
  { id: "weekly-sun", label: "Hebdomadaire (dimanche 02h00)", calendar: "Sun *-*-* 02:00:00" },
];

export const COMMON_BACKENDS = [
  { id: "drive", label: "Google Drive (optimisé OAuth perso)" },
  { id: "crypt", label: "Crypt (chiffrement sur Google Drive ou autre)" },
  { id: "s3", label: "S3 / MinIO / Wasabi / Cloudflare R2" },
  { id: "sftp", label: "SFTP (SSH)" },
  { id: "smb", label: "SMB / CIFS (partage NAS)" },
  { id: "webdav", label: "WebDAV / Nextcloud" },
  { id: "b2", label: "Backblaze B2" },
  { id: "onedrive", label: "Microsoft OneDrive" },
  { id: "dropbox", label: "Dropbox" },
  { id: "union", label: "Union (fusion de remotes)" },
  { id: "alias", label: "Alias (raccourci de chemin)" },
];

export function formatBytes(bytes) {
  const n = Number(bytes);
  if (!Number.isFinite(n) || n < 0) return "—";
  if (n === 0) return "0 o";
  const units = ["o", "Ko", "Mo", "Go", "To", "Po"];
  const idx = Math.min(Math.floor(Math.log(n) / Math.log(1024)), units.length - 1);
  const val = n / Math.pow(1024, idx);
  return `${val >= 100 ? val.toFixed(0) : val >= 10 ? val.toFixed(1) : val.toFixed(2)} ${units[idx]}`;
}

export function formatSpeed(bytesPerSec) {
  const n = Number(bytesPerSec);
  if (!Number.isFinite(n) || n <= 0) return "0 o/s";
  return `${formatBytes(n)}/s`;
}

export function formatEta(seconds) {
  const s = Number(seconds);
  if (!Number.isFinite(s) || s < 0) return "—";
  if (s < 60) return `${Math.round(s)} s`;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const rem = Math.round(s % 60);
  if (h > 0) return `${h} h ${String(m).padStart(2, "0")} min`;
  return `${m} min ${String(rem).padStart(2, "0")} s`;
}

export function slugifyId(text) {
  return String(text || "")
    .toLowerCase()
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48);
}

export function joinRclonePath(base, child) {
  const cleanChild = String(child || "").replace(/^\/+/, "");
  if (!base) return cleanChild;
  if (base.endsWith(":")) return `${base}${cleanChild}`;
  if (base.endsWith("/")) return `${base}${cleanChild}`;
  return `${base}/${cleanChild}`;
}

export function parentRclonePath(target) {
  const raw = String(target || "").trim();
  if (!raw) return "";
  if (raw.startsWith("/")) {
    const trimmed = raw.replace(/\/+$/, "");
    if (!trimmed || trimmed === "/") return "/";
    const idx = trimmed.lastIndexOf("/");
    return idx <= 0 ? "/" : trimmed.slice(0, idx);
  }
  const colon = raw.indexOf(":");
  if (colon < 0) return raw;
  const prefix = raw.slice(0, colon + 1);
  const rest = raw.slice(colon + 1).replace(/\/+$/, "");
  if (!rest) return prefix;
  const idx = rest.lastIndexOf("/");
  return idx < 0 ? prefix : `${prefix}${rest.slice(0, idx)}`;
}

export function computePercent(done, total) {
  const d = Number(done);
  const t = Number(total);
  if (!Number.isFinite(d) || !Number.isFinite(t) || t <= 0) return 0;
  return Math.max(0, Math.min(100, Math.round((d / t) * 100)));
}
