# AlphaNet MailOps production operations

## Live services

- Admin: <https://mailops.alphanetplus.com>
- Private download gateway: <https://download.alphanetplus.com>
- Health: <https://mailops.alphanetplus.com/api/health>
- Sender: `DLSS5 Studio <downloads@mail.dlss5nvidia.com>`
- Host service: `alphanet-mailops.service`
- Tunnel service: `cloudflared-alphanet-mailops.service`

The health response must report `mailConfigured`, `firebaseConfigured`, and
`artifactStorageConfigured` as `true`.

## Build targets

The build form accepts either:

- an `https://` object-storage URL; or
- `artifact:filename.zip` for a file already stored in
  `/opt/alphanet-mailops/data/artifacts/` on the Alibaba Cloud host.

Local artifacts are never served by a static public path. A recipient opens a
time-limited token URL, reviews the build/checksum page, and submits the
download form. Only that submit consumes a download. This prevents email link
scanners from using a recipient's allowance.

## Replacing the verification build

`MailOps Delivery Verification 1.0.0` proves the system but is not the DLSS5
Studio application. When the authorised Windows package is available:

1. Upload it to private object storage and register the HTTPS URL, or copy it
   into the server artifact directory and use `artifact:filename.zip`.
2. Add the SHA-256 value to the build record.
3. Send one delivery to the administrator account and verify the checksum.
4. Activate the real build and pause `MailOps Delivery Verification 1.0.0`.

Do not label the verification ZIP as a Studio release. It contains only a
README and machine-readable manifest.

## Production verification — 2026-10-02

- Resend sending domain: verified.
- Resend delivery to the administrator account: delivered.
- Intake to MailOps: accepted.
- Active build visible through the admin API: 1.
- Private landing page: HTTP 200 without consuming a download.
- First and second submitted downloads: HTTP 200 and exact SHA-256 match.
- Third submitted download: HTTP 410 as configured.
- MailOps and Cloudflare Tunnel systemd services: active.

## Data to back up

- `/opt/alphanet-mailops/data/mailops.db`
- `/opt/alphanet-mailops/data/artifacts/`
- `/opt/alphanet-mailops/.env` (secret; keep encrypted and never commit)
