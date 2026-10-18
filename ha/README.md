# Felt for Home Assistant

A custom integration that makes Felt a Home Assistant **conversation agent**:
what Mario says to Assist (typed or spoken, in the HA app on any device) goes
to felt-core's `/v1/ha/chat`, and her reply comes back. Workspace
`runbooks/felt-ha-hub.md`, decision D3.

- **Auth**: Authentik client credentials - the `ha-felt` service account and
  its app password (clockmost-infra `projects/authentik/blueprints/ha-felt.yaml`).
  The integration gets 15-minute access tokens and refreshes them itself;
  felt-core checks them (`src/felt_core/ha_auth.py`).
- **Route**: `https://felt-ha.clockmost.com` - LAN/VPN only, that one path.

- **Felt panel** (0.4.0, 2026-10-19): a "Felt" sidebar page (admin only) with
  the Felt app's Talk and Margins - felt-dashboard's HA build in an iframe of
  its own. The page asks this integration over HA's websocket
  (`felt/app/call`, `felt/app/stream`, `panel.py`), the integration calls
  felt-core's `/v1/ha/app/*` with its token. Controls, lists and systems are
  native HA already.

## Install

Build the panel in felt-dashboard (`pnpm build:panel`) and copy
`dist-panel/*` into `custom_components/felt/panel/` (not in git). Copy
`custom_components/felt/` into HA's `/config/custom_components/felt/`,
restart HA, then Settings -> Devices & services -> Add integration -> Felt
(defaults are right; paste the app password). Then Settings -> Voice
assistants -> pick "Felt" as the conversation agent.
