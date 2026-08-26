# Portable deployment

## Personal Windows computer

Install Python 3.12 and Node.js, create a fresh virtual environment, install the
project, and build the frontend with npm ci and npm run build. Start with
scripts/start_windows.ps1 and open http://127.0.0.1:8080.

Windows defaults to deepseek_only. No PPU endpoint is initialized. The API key is
kept in browser sessionStorage, never localStorage, the database, or the
deployment manifest. Deployment-management routes accept loopback clients only.

## Linux PPU server

Copy a clean project directory without .env, databases, caches, models, or logs.
Install the validated PPU runtime, SGLang, Python 3.12, and Qwen model separately.
Copy deploy/linux-ppu.env.example to a protected environment file and adjust paths.
Put SQLite and deployment state on a local filesystem such as /var/tmp, never
OSSFS, NFS, or CIFS. Start with scripts/start_linux.sh or the supplied systemd unit.

Linux defaults to ppu_local. Existing endpoints are reused only after both their
health endpoint and advertised model identity match; they are never restarted by a
migration. Endpoint-only startup does not require a local model directory. A new
managed launch additionally requires server capability checks, plan preview,
explicit confirmation, and model artifact validation.

The management API is loopback-only even when the review API listens on 0.0.0.0.
Operate it on the server itself or through an SSH tunnel. The supplied launchers
force one gateway worker because issued plans and the in-process apply lock belong to
one worker; do not replace this with a multi-worker Uvicorn command.

## Reliability and rollback

Deployment plans contain a random server-issued nonce, expire after ten minutes,
cannot be replayed, and are serialized with a process lock. The current manifest is backed up before
application. If a managed local launch fails, the previous manifest is restored.
Manifest writes are flushed and atomically replaced. Applying a plan is disabled
unless CODE_REVIEW_DEPLOYMENT_APPLY_ENABLED=true is set for an authorized window.

Restart the gateway after a successful mode change so cached providers are rebuilt.
Managed SGLang instances start in independent process sessions. The supplied systemd
unit uses KillMode=process intentionally, so restarting or stopping only the gateway
does not terminate verified model instances. After restart, run
python scripts/manage_ppu_model.py status and confirm every tracked instance is alive.
To stop only instances recorded by this tool, explicitly run
python scripts/manage_ppu_model.py stop; it checks PID command lines, model path, and
ports before sending signals.
Preserve SQLite with the SQLite backup API or a stopped-gateway copy; never copy
only the main file while WAL writes are active.

Real PPU readiness still requires a live structured-inference smoke test on the
target hardware. Offline tests cannot prove driver, SDK, device, or model compatibility.
