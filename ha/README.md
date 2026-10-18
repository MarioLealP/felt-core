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

## Install

Copy `custom_components/felt/` into HA's `/config/custom_components/felt/`,
restart HA, then Settings -> Devices & services -> Add integration -> Felt
(defaults are right; paste the app password). Then Settings -> Voice
assistants -> pick "Felt" as the conversation agent.
