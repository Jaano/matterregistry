# Matter Registry

> A self-hosted registry for the metadata Matter discards.

![Devices list](docs/screenshots/devices.png)

![Device detail](docs/screenshots/detail.png)

## What it does

The Matter smart-home standard deliberately destroys commissioning secrets after first pairing - pairing PINs, QR codes, setup codes, and manual pairing codes are considered single-use. **Matter Registry fills that gap.** It stores and displays the out-of-band metadata for your Matter devices: pairing PINs, manual setup codes, QR payloads, install photos, purchase and warranty dates, firmware notes, and more.

Matter Registry can regenerate the original QR code and 11-digit manual pairing code from a stored payload, so you can re-commission a device without hunting through packaging. It also prints QR stickers - one device, or a full sheet of all of them - onto Avery L7162 (A4) or 5162 (US Letter) label stock, chosen under **Settings → Printing**. A full-database JSON export can be restored on a fresh install - bringing every device, credential, and photo with it.

Access is intentionally unauthenticated at the application layer. The design goal is to be a private, LAN-local tool protected by your network and HA Ingress, not one that adds its own login screen to a home dashboard.

## Install - Home Assistant App

1. Open Home Assistant → **Settings → Add-ons → ⋮ (overflow menu) → Repositories**.
2. Add the store URL: `https://github.com/Jaano/matterregistry`
3. Refresh the store list, find **Matter Registry**, and click **Install**.
4. Start the App, then click **Open Web UI**.

The App runs behind HA Ingress - no port forwarding or reverse proxy required. All URLs are ingress-aware.

## Install - standalone Docker

Pull and run with Docker Compose. Create a `docker-compose.yml`:

```yaml
services:
  matterregistry:
    image: ghcr.io/jaano/matterregistry:latest
    container_name: matterregistry
    ports:
      - "5591:5591"
    volumes:
      - ./data:/config
    environment:
      - MR_LOG_LEVEL=info
    restart: unless-stopped
```

Then:

```bash
docker compose pull && docker compose up -d
```

Open `http://localhost:5591`. All data is in `./data/matterregistry.db` (a single SQLite file).

> **In-browser QR scanning requires HTTPS** (a browser secure-context rule for `getUserMedia`). Plain `http://` works for every other workflow - manual entry, paste of an `MT:` payload, JSON import, the REST API - so dev and testing are unaffected. To use the camera-scan flow in a standalone deployment, put a TLS-terminating reverse proxy in front of the container or run on `localhost`. The HA App path is HTTPS out of the box via HA Ingress.

## Backup & restore

**Settings → Download full backup** exports a single JSON file containing all devices, credentials, and attachment images as base64. The file contains pairing PINs in plain text - treat it like a password file and store it somewhere only you can read.

To restore: **Settings → Restore from backup**, pick the file, choose a collision policy (skip or replace), apply.

## What it's not

- **Not encrypted at rest.** The SQLite database stores pairing PINs in plain text. The whole-database export does the same. Protect the data directory and the export file with filesystem permissions.
- **Not authenticated at the application layer.** Access control is delegated to HA Ingress (for the HA App path) or a reverse proxy (for standalone). Do not expose port 5591 to the public internet without a proxy in front.
- **In-browser QR scanning needs HTTPS.** Camera capture relies on a browser API that's only available in a secure context. HA Ingress provides HTTPS automatically; for standalone, terminate TLS at a reverse proxy. Manual entry, paste, and JSON import work the same over plain HTTP, so this only affects the scan path.

## License

[Apache License 2.0](LICENSE)
