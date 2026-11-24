/**
 * api.js — pont entre l'interface Cockpit et /usr/lib/cockpit-rclone/rclone-ctl.py
 * via cockpit.spawn (zéro secret dans argv : payloads sensibles envoyés sur stdin).
 */

const CTL_BIN = "/usr/lib/cockpit-rclone/rclone-ctl.py";

export function callCtl(args, payload = null) {
  if (typeof window === "undefined" || !window.cockpit) {
    return Promise.reject(new Error("API Cockpit introuvable (ouvrez cette page depuis Cockpit)."));
  }
  const proc = window.cockpit.spawn(["/usr/bin/python3", CTL_BIN, ...args], {
    superuser: "try",
    err: "message",
  });
  if (payload !== null && payload !== undefined) {
    proc.input(JSON.stringify(payload));
  }
  return new Promise((resolve, reject) => {
    let stdout = "";
    proc.stream((chunk) => {
      stdout += chunk;
    });
    proc.then(() => {
      try {
        resolve(stdout.trim() ? JSON.parse(stdout) : { ok: true });
      } catch (err) {
        reject(new Error(`Réponse JSON invalide : ${stdout.slice(0, 300)}`));
      }
    }).catch((err, data) => {
      let msg = err && err.message ? err.message : String(err);
      for (const candidate of [data, msg, stdout]) {
        if (!candidate) continue;
        try {
          const parsed = JSON.parse(String(candidate));
          if (parsed && parsed.error) {
            msg = parsed.error;
            break;
          }
        } catch {
          // texte brut
        }
      }
      reject(new Error(msg));
    });
  });
}
