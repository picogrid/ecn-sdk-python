# Picogrid ECN Operator View

Picogrid ECN Operator View turns authorized entity and location observations into a
live local map. Credentialed operators can inspect fresh and stale tracks,
detections, PLI, and other supported entities, then deliberately send an allowlisted
task when tasking has been enabled for the deployment.

The application consumes the installed `picogrid-ecn-client` wheel and defaults to a
read-only offline demo. Live mode requires TLS with server-certificate verification,
one selected mTLS or bearer authentication profile, and explicit integration and
category allowlists. The local browser server does not become an ECN endpoint, and
the application performs no
topic discovery, command discovery, automatic task publication, or automatic task
retry.

## Install and run

Install the inspected client and operator wheels into your Python environment. The
operator wheel already contains the compiled browser application; Node.js, npm, a
repository clone, and a second development server are not runtime dependencies.

```console
python -m pip install /path/to/picogrid_ecn_client-0.1.0-py3-none-any.whl # x-release-please-version
python -m pip install /path/to/picogrid_ecn_operator_app-0.1.0-py3-none-any.whl # x-release-please-version
picogrid-ecn operator --demo
picogrid-ecn-operator --demo
```

Open `http://127.0.0.1:8080`. Mock state contains newly generated synthetic UUIDs,
moving entity and location-only markers, bounded diagnostics, and an exact local
task handler. No operational endpoint or credential is used.

`--demo` always starts read-only, even if tasking variables are present in the shell.
Task-flow development uses the checked-in test harness; live tasking requires the
explicit policy described below. Task controls still begin disarmed in every browser,
and the app never retries a task publication.

Frontend contributors use `npm ci --ignore-scripts` and `npm run build` from this
source directory. Those commands are construction checks only. Building the operator
wheel runs the same locked frontend build and embeds the result as package data.
To exercise the task confirmation UI in mock mode, set
`OPERATOR_TASKING_ENABLED=true` and set `OPERATOR_TASK_ENTITY_ALLOWLIST` to one or
more dedicated canonical synthetic UUIDs. The mock runtime selects the smallest
allowlisted UUID as `synthetic-task-target`; the checked-in test harness uses
`00000000-0000-4000-8000-000000000201`. Other mock entities use generated UUIDs,
and `--demo` remains read-only. Task controls still begin disarmed in each browser.
Selecting the synthetic task target, preparing an allowlisted command, reviewing its
complete payload, and checking the confirmation box are all required before the app
sends exactly one task.

## Live authentication configuration

Use the credentials you were provided. If you are an authorized user and do not have
credentials, contact your Picogrid Deployments or Engineering contact. Keep all
authentication material outside application and source directories. Configure the
SDK profile with `picogrid-ecn configure`; mTLS and bearer profiles may use different
MQTT listeners, so their ports are not interchangeable.

The preferred path is an SDK profile plus operator-specific observation allowlists.
After those allowlists are set, start the installed application with either entry
point; the two commands are alternatives, not a sequence:

```bash
export OPERATOR_ECN_INTEGRATION_ALLOWLIST=integration-a,integration-b
export OPERATOR_ECN_CATEGORY_ALLOWLIST=TRACK,DETECTION
picogrid-ecn operator --profile NAME
# Or, equivalently, the operator package's own console entry point:
picogrid-ecn-operator --profile NAME
```

`ECN_PROFILE=NAME picogrid-ecn-operator` is equivalent. Connection settings and
credentials come from that profile; operator integration, category, entity, and
command allowlists remain separate and cannot be supplied by the connection profile.
Do not also set the legacy `OPERATOR_ECN_CLIENT_INTEGRATION` or
`OPERATOR_ECN_WIRE_FORMAT` overrides when using a profile.

Before any live launch, obtain written authorization that names the exact ECN target
and each permitted operation. Observation authorization does not authorize task
publication; live tasking additionally requires written authorization for the exact
target UUID and command.

For environment-only automation, provide:

```text
OPERATOR_MODE=live
OPERATOR_ECN_HOST=<authorized MQTT hostname>
OPERATOR_ECN_MQTT_PORT=<issued-mtls-listener-port>
OPERATOR_ECN_CA_CERT=<absolute external CA path>
OPERATOR_ECN_INTEGRATION_ALLOWLIST=<explicit integrations>
OPERATOR_ECN_CATEGORY_ALLOWLIST=TRACK,DETECTION
```

Select exactly one authentication profile. For mTLS (the default if the selector is
omitted), provide:

```text
OPERATOR_ECN_AUTH=mtls
OPERATOR_ECN_CLIENT_CERT=<absolute external certificate path>
OPERATOR_ECN_CLIENT_KEY=<absolute external private-key path>
```

For a deployment that has explicitly assigned an MQTT username and bearer token,
provide the bearer listener port as well as:

```text
OPERATOR_ECN_AUTH=bearer
OPERATOR_ECN_MQTT_PORT=<deployment-issued-bearer-listener-port>
OPERATOR_ECN_MQTT_USERNAME=<deployment-issued-mqtt-username>
OPERATOR_ECN_BEARER_TOKEN=<deployment-issued-token>
```

After setting the common variables and exactly one authentication profile, launch
`picogrid-ecn-operator`.

The bearer profile passes the assigned username and current token to the installed
SDK, which places the token in the MQTT v5 CONNECT password field. The application
holds the configured token as a secret value; it does not infer a username, scopes,
or authorization from the token, write it to diagnostics, or proactively refresh it.
Replace an expiring token through the deployment's credential lifecycle and restart
the application. The target deployment must separately confirm its assigned username
and acceptance of this profile.

Setting bearer variables with the mTLS profile, or client certificate/key variables
with the bearer profile, fails closed. A server-auth TLS handshake may still succeed
when the wrong profile listener is selected before CONNACK rejects authentication;
do not add fallback credentials. Remote plaintext, unverified TLS, empty
allowlists, unknown entity categories, and unsupported command schemas also fail
closed. The app never reads an ECN HTTP endpoint. Broker ACLs remain the authorization
boundary; local configuration does not claim server-granted scopes.

Tasking is off unless both `OPERATOR_TASKING_ENABLED=true` and a validated
`OPERATOR_COMMANDS_FILE` are provided. The command file is a closed allowlist: each
command names allowed integrations, either `complete` (the default) or explicitly
configured `acknowledgment` response mode, and a bounded scalar JSON request schema.
Fire-and-forget mode is rejected. An acknowledgment is exactly one immediate response;
handler completion is intentionally not reported. The sample file is synthetic and
should not be treated as deployed command discovery.
Every tasking mode also requires `OPERATOR_TASK_ENTITY_ALLOWLIST`, containing the exact
canonical UUID of every authorized target. An observed entity is never implicitly
task-authorized.

`OPERATOR_ALLOWED_HOSTS` and `OPERATOR_ALLOWED_ORIGINS` constrain the browser-facing
Host and Origin boundaries. Mutation bodies require one valid `Content-Length` and
are rejected above 20 KiB. Task routes also require their explicit intent header;
validation failures return a generic response that does not echo rejected input.
The browser API has no user authentication or per-user authorization. It is a trusted
single-user, local-host surface: loopback binding plus Host and Origin checks reduce
accidental exposure, but they do not authenticate another process running as the same
local user. Do not expose it as a shared or multi-user service.
Use the packaged `picogrid-ecn-operator` launcher; mounting `operator_app.app:app`
under another application server is unsupported because it bypasses the launcher's bind
check.

### Optional basemap

The default map is a bundled WGS 84 graticule: it makes no tile request and remains
useful when offline. An operator may opt into a separately reviewed tile source with
both variables below:

```text
OPERATOR_BASEMAP_URL_TEMPLATE=https://tiles.example.invalid/{z}/{x}/{y}.png
OPERATOR_BASEMAP_ATTRIBUTION=Authorized map data provider
```

The URL must be HTTPS or a root-relative local path and contain each of `{z}`, `{x}`,
and `{y}` exactly once. User information, query strings, fragments, traversal, and
plain HTTP are rejected, so API keys cannot be smuggled into browser configuration.
Attribution is required, rendered as plain text, and bounded. The app removes a
configured layer after repeated tile errors and clearly returns to the offline
graticule; no remote tile endpoint is supplied by this repository.

## Container

Place the exact inspected client and operator wheels under `wheelhouse/`; that
directory is ignored by Git. Pass only their filenames as `CLIENT_WHEEL` and
`OPERATOR_WHEEL`. Build and run mock mode with:

```bash
cp .env.example .env
CLIENT_WHEEL=<exact-client-wheel-filename> OPERATOR_WHEEL=<exact-operator-wheel-filename> docker compose up --build operator-mock
```

The mTLS Compose profile bind-mounts external credential files read-only. Set the
three `OPERATOR_*_HOST` paths plus numeric `OPERATOR_CONTAINER_UID` and
`OPERATOR_CONTAINER_GID`, then use `docker compose --profile live up operator-live`.
For bearer authentication, set the explicit username and token only in the ignored
runtime `.env`, set the CA host path and numeric IDs, then use
`docker compose --profile live-bearer up operator-live-bearer`; this profile mounts
only the CA. Both services bind only to loopback, run with a read-only filesystem and
no-new-privileges, and do not place credentials in the image or build context.

## Lifecycle and bounds

- Startup connects with MQTT v5, waits for the public watcher SUBACKs, and never
  publishes in live mode.
- Entity subscriptions are the category/integration cross-product requested in the
  environment. Location subscriptions use each allowed integration plus one UUID
  wildcard level, so location-only UUIDs remain observable without a broad tree.
- Entity and location observation ages are tracked independently, so a fresh
  location cannot make stale entity metadata eligible for tasking.
- Cache size, event buffers, browser count, browser queues, diagnostics, task
  outcomes, prepared-task count, and preparation lifetime are all bounded.
- A preparation remains bound to its canonical integration/UUID while ordinary fresh
  observations for that same entity continue. Selection changes, staleness, policy or
  readiness loss, and expiry invalidate it; confirmation revalidates eligibility
  before the one permitted MQTT send.
- Prepared-task dismissal keeps review state open until the backend confirms
  invalidation. A failed invalidation strands the open, disabled review, disarms
  tasking, drops the browser view, and requires the explicit
  **Reconnect the operator view** action. Discard and view-retirement requests are
  bounded to ten seconds; confirmation is bounded to twenty seconds so it outlasts
  the fifteen-second backend task exchange. A timeout or unconfirmed invalidation
  reports an unknown delivery outcome, and task publication is never retried. The
  backend keeps only a bounded,
  expiry-limited proof for tokens it invalidated before publication, so a late
  prepare response or a lost successful discard response can confirm retirement
  while a consumed or unknown token still fails closed.
- Browser fanout coalesces updates. A disconnected view does not automatically
  reconnect; the operator must choose **Reconnect the operator view**. Before
  opening a successor
  state WebSocket, the browser requires the local backend to acknowledge retirement
  of the exact prior view generation. A mismatch, active mutation, unknown retirement
  outcome, or post-ack duplicate refusal leaves tasking disabled and opens no further
  successor. This local lifecycle request never reaches MQTT. Any locally prepared
  task is discarded and task controls are disarmed on view, MQTT, watcher, expiry, or
  freshness loss, so reconnecting always requires a fresh prepare/review step.
- Shutdown cancels watcher and synthetic tasks, unregisters the mock handler, closes
  every client and broker, clears observed state, and closes browser queues.

The map uses locally bundled Leaflet and local Picogrid brand assets. Standalone
locations, including Position Location Information (PLI), appear as location markers
until an entity event supplies their category. Entity categories
use independently authored lettered geometric markers; affiliation uses both color and
text in the legend; stale state uses a dashed, desaturated marker; selection adds a
high-contrast ring; and connection loss visibly de-emphasizes the map. These generic
symbols are not military-standard or proprietary tactical symbology. A configured
basemap is visual context only and never changes watcher or task topic scope.

## Mock screenshots

The source guide contains deterministic desktop-light, desktop-dark,
mobile-light, and mobile-dark captures of the same synthetic mock state. The
installed wheel contains only the runtime browser assets, not documentation
screenshots.

## Production-use checklist

- Use only inspected, matching client and operator wheels.
- Obtain written authorization naming the exact ECN target and live operations before
  connecting; obtain separate written authorization for each task target UUID and
  command before enabling tasking.
- Keep tasking disabled until integration, command, and exact target UUID allowlists
  receive an independent safety review.
- Keep the HTTP port bound to host loopback; wildcard binding is permitted only
  inside the container with its explicit container-bind guard.
- Treat the browser boundary as a trusted single-user/local-host surface. It has no
  login or per-user authorization, and loopback, Host, and Origin controls do not
  authenticate another local process.
- Configure exact Host and Origin allowlists for the browser entry point.
- Mount CA and, for mTLS, certificate and private-key material read-only from outside
  every source tree and build context; verify file ownership and least-readable
  permissions.
- For bearer authentication, use only the deployment-assigned MQTT username, keep the
  token out of shell history and retained logs, and rotate or replace it through the
  deployment credential lifecycle before expiry. Do not infer scopes from it.
- Confirm broker ACLs independently. Local allowlists do not prove ECN authorization.
- Monitor MQTT connection state, watcher activity, entity/location/browser drop
  and decode-error counters, stale observations, and distinct acknowledgment,
  success, timeout, cancellation, failure, and reconnect outcomes.
- Decide whether browser-visible entity metadata is appropriate for the operators and
  physical environment before deployment.
- Exercise mock cleanup and bounded staging validation separately; mock evidence is
  not deployed compatibility evidence.
- Stop the process deterministically and verify that browser clients, ECN clients,
  subscriptions, mock tasks, temporary work directories, and credential mounts are
  gone.
