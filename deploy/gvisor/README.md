# Discovery gVisor host setup

Discovery connector code must never run until this host setup is complete.

1. Install a reviewed gVisor `runsc` release on the Docker host at
   `/usr/local/bin/runsc`.
2. Merge `daemon.json` into `/etc/docker/daemon.json`, preserving any existing
   daemon settings. The configured platform must remain `systrap`; this project
   does not require `/dev/kvm`.
3. Restart Docker and verify `docker info --format '{{json .Runtimes}}'` contains
   `runsc`. The application deliberately fails closed when it does not.
   Set `DISCOVERY_SANDBOX_RUNSC_VERSION` to the exact reviewed release string
   printed after `runsc version` (for example `release-YYYYMMDD.N`). A missing or
   mismatched version blocks all sandbox execution.
4. Build the Runtime from `backend/` with
   `crawler-runtime/build-runtime.sh`. The reviewed base is the pinned Python
   3.12 slim image; the Runtime installs only Playwright's Chromium headless
   shell. Firefox, WebKit, and full Chromium are intentionally absent. The base
   image argument must include an `@sha256:` digest. Push the versioned tag to the internal registry,
   then record the resulting registry digest (not a mutable tag) in the JSON
   `DISCOVERY_SANDBOX_RUNTIME_IMAGES` environment setting, for example:

   ```text
   {"crawler-runtime:1":"registry.internal/os-news/crawler-runtime@sha256:<64 hex>"}
   ```

   A single-host development deployment may instead use a registry bound only
   to loopback. Tag and push the image to (for example)
   `localhost:5000/os-news/crawler-runtime:1`, then configure the resulting
   `localhost:5000/...@sha256:<digest>` reference. Keep the local registry data
   and the digest-addressed image on that Docker host. This is a development
   convenience only; multi-host deployments still require an approved internal
   registry so every execution host can pre-pull the identical digest.

5. Pull that digest onto the execution host before starting the backend. Runtime
   pulls are disabled. The trusted backend needs the Docker CLI and permission to
   reach the Docker daemon; connector and proxy containers never receive the
   socket, project source, database volumes, or application secrets.
   When the backend itself runs in a container, also mount the host's reviewed
   `/usr/local/bin/runsc` and `/etc/docker/daemon.json` read-only at the same
   paths. Startup validation executes the binary, requires its version output to
   identify `runsc`, and proves the daemon config uses `--platform=systrap`;
   merely naming a `runc` alias `runsc` is rejected.

Each connector gets a temporary internal-only network. A trusted per-job proxy is
the only container connected to both that network and Docker's egress bridge. It
enforces exact Manifest domains, pins validated public DNS answers, rejects
private/reserved/loopback/metadata addresses, checks HTTP Host and TLS SNI, and
therefore rejects redirects to domains that have not passed review.

After setup, run a reviewed smoke connector before enabling scheduled execution.
If the backend reports that `runsc` is missing, do not change the runtime setting
to `runc`; install/register gVisor and retry.
