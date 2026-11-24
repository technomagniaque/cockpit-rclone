import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  computePercent,
  formatBytes,
  formatEta,
  formatSpeed,
  joinRclonePath,
  parentRclonePath,
  slugifyId,
} from "../cockpit/profiles.js";

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

test("formatage octets, vitesses, ETA et pourcentages", () => {
  assert.equal(formatBytes(0), "0 o");
  assert.equal(formatBytes(1024), "1.00 Ko");
  assert.equal(formatBytes(128 * 1024 * 1024), "128 Mo");
  assert.equal(formatSpeed(8.5 * 1024 * 1024), "8.50 Mo/s");
  assert.equal(formatEta(45), "45 s");
  assert.equal(formatEta(3665), "1 h 01 min");
  assert.equal(computePercent(50, 200), 25);
  assert.equal(computePercent(10, 0), 0);
});

test("manipulation de chemins rclone et slugifyId", () => {
  assert.equal(joinRclonePath("gdrive:", "NAS/p1"), "gdrive:NAS/p1");
  assert.equal(joinRclonePath("/H1/p1", "photos"), "/H1/p1/photos");
  assert.equal(parentRclonePath("gdrive:NAS/p1/photos"), "gdrive:NAS/p1");
  assert.equal(parentRclonePath("gdrive:NAS"), "gdrive:");
  assert.equal(parentRclonePath("/H1/p1/photos"), "/H1/p1");
  assert.equal(parentRclonePath("/H1"), "/");
  assert.equal(slugifyId("Sauvegarde H1/p1 → GDrive!"), "sauvegarde-h1-p1-gdrive");
});

test("index.html respecte strictement la CSP Cockpit (aucun script ni handler inline)", () => {
  const html = fs.readFileSync(path.join(REPO, "cockpit/index.html"), "utf-8");
  assert.doesNotMatch(html, /<script(?![^>]*\bsrc=)[^>]*>/i, "script inline interdit par la CSP Cockpit");
  assert.doesNotMatch(html, /<[^>]*\son[a-z]+\s*=/i, "gestionnaire onclick/onchange inline interdit");
});

test("manifest.json valide et cohérent", () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(REPO, "cockpit/manifest.json"), "utf-8"));
  assert.ok(manifest.menu && manifest.menu.rclone);
  assert.equal(manifest.menu.rclone.path, "index.html");
});
