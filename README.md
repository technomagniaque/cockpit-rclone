# cockpit-rclone — Extension Cockpit pour rclone (NAS ZFS ↔ Google Drive & montage bureau Z640)

Extension Cockpit complète et sans dépendance lourde (HTML/CSS PatternFly clair & sombre + ES modules + backend Python 3 + services/timers systemd) conçue pour :
- **`debian13zfs1`** (`192.168.0.9`) : synchronisation et migration multi-To entre le pool ZFS `H1` (`/H1/d1`, `/H1/p1`, `/H1/p2b`…) et **Google Drive** ;
- **`debian13z640eti`** (`192.168.0.67`) : **montage FUSE fiable de Google Drive** (profil `gdrive-z640-desktop` avec cache VFS complet sur NVMe, `uid`/`gid` utilisateur pour KDE Dolphin / LibreOffice, rafraîchissement ChangeNotify 30 s et démontage propre `fusermount3 -uz`).

## Fonctionnalités
1. **Tableau de bord & Transferts temps réel** : suivi en direct via l'API `--rc` locale authentifiée (`127.0.0.1`, mot de passe aléatoire par run en mode `0600`), changement de `--bwlimit` à chaud, arrêt propre, quotas `rclone about` et liste des datasets ZFS.
2. **Remotes & Assistant Google Drive / Crypt** :
   - Détection automatique des identifiants OAuth Google déjà créés (`/home/eti/.config/mygdoc/oauth-credentials.json` sur Z640, ou `/etc/cockpit-rclone/google-oauth.json` sur le NAS) : le `client_secret` est lu côté serveur et écrit en `0600` dans `/root/.config/rclone/rclone.conf` sans jamais transiter par le navigateur.
   - Support de `crypt` (chiffrement transparent via `rclone obscure`) et de tous les autres backends `rclone` (S3, SFTP, SMB, WebDAV, B2, OneDrive, Dropbox, Union…).
3. **Sync, Migration & Planification systemd** :
   - Modes `copy`, `sync` (avec `--backup-dir` horodaté anti-suppression), `move`, `bisync`, `check`, `dedupe` et simulation `--dry-run` en 1 clic.
   - **5 profils Google Drive** : migration gros fichiers (`128M` chunks, `fast-list`, `tpslimit 10`, `--drive-stop-on-upload-limit`), petits fichiers, continu 24/7 sous plafond 750 Go/j (`8.5M`), rapatriement GDrive → NAS (`multi-thread-streams 4`), personnalisé.
   - **Snapshot ZFS atomique** optionnel avant transfert (`.zfs/snapshot/cockpit-rclone-...`) avec destruction garantie dans un bloc `finally`.
   - Planification par `systemd timers` (`OnCalendar=`) et consultation des journaux `journalctl`.
4. **Explorateur double panneau** : navigation NAS ZFS ↔ remotes (`lsjson`), taille (`size`), création/suppression de dossiers et création directe d'une tâche depuis les deux dossiers ouverts.
5. **Montages FUSE persistants (`cockpit-rclone-mount@.service`)** : profils `gdrive-z640-desktop` et `gdrive-nas-ro`, `user_allow_other` activé par le paquet `.deb`, redémarrage auto sur panne réseau, nettoyage des montages *stale* avant montage.
6. **Serve (`rclone serve`) & Maintenance** : WebDAV / HTTP / SFTP / DLNA gérés par systemd, nettoyage des snapshots ZFS orphelins.

## Construction du paquet `.deb`
```bash
python3 packaging/build_deb.py --out dist
```

## Installation (une fois la maintenance terminée)
### Sur `debian13zfs1` (`192.168.0.9`)
```bash
scp dist/cockpit-rclone_1.0.0_all.deb root@192.168.0.9:/tmp/
# Optionnel : réutiliser la clé OAuth Google déjà créée sur le Z640 (mode 0600)
scp /home/eti/.config/mygdoc/oauth-credentials.json root@192.168.0.9:/etc/cockpit-rclone/google-oauth.json
ssh root@192.168.0.9 'chmod 600 /etc/cockpit-rclone/google-oauth.json && apt update && apt install -y /tmp/cockpit-rclone_1.0.0_all.deb && systemctl restart cockpit.socket'
```

### Sur `debian13z640eti` (montage fiable de Google Drive)
```bash
sudo apt update && sudo apt install -y ./dist/cockpit-rclone_1.0.0_all.deb
sudo systemctl restart cockpit.socket
```
Dans Cockpit (`https://127.0.0.1:9090` → **Rclone & Google Drive**) :
1. Onglet **Remotes** : sélectionnez la clé OAuth détectée (`/home/eti/.config/mygdoc/oauth-credentials.json`), cliquez sur **Afficher la commande `rclone authorize`**, lancez-la dans un terminal pour ouvrir Brave et collez le token JSON obtenu.
2. Onglet **Montages FUSE** : créez `gdrive-z640` avec le profil **Bureau fiable Z640**, remote `gdrive:`, point de montage `/home/eti/GoogleDrive`, propriétaire `eti (1000:1000)`, puis **Monter** et **Activer boot**.

## Tests
```bash
python3 -m unittest discover -s tests -v
node --test tests/test_frontend.mjs
```
