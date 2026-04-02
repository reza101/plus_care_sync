# Plus Care Sync

A Frappe/ERPNext application for syncing data between a local ERPNext server and a live server. Built for pharmacy branches that operate offline or with limited connectivity and need to stay in sync with a central server.

## Features

- **Bidirectional Sync** — push from local to live, pull from live to local, or both
- **Selective Sync** — choose specific modules or DocTypes to sync rather than the full database
- **Conflict Resolution** — configurable strategies: live wins, local wins, latest timestamp wins, or manual review
- **Scheduled or Manual** — run sync on a schedule or trigger it manually from the settings page
- **Sync Logs** — full history of every sync operation with error details

## Requirements

- ERPNext v15
- Frappe v15
- Network access between local and live servers (for manual/scheduled sync)

## Installation

```bash
cd /path/to/your/bench
bench get-app https://github.com/your-org/plus_care_sync --branch main
bench --site yoursite.localhost install-app plus_care_sync
bench --site yoursite.localhost migrate
```

## Setup

### On the live server

1. Go to **User Settings → API Access**
2. Generate an API Key and API Secret for a System Manager user
3. Copy both values

### On the local server

1. Go to **Setup → Sync Settings**
2. Enter the live server URL
3. Paste the API Key and Secret
4. Choose sync direction and data scope
5. Click **Test Connection** to verify
6. Click **Sync Now** to run the first sync

## Sync Options

| Option | Description |
|---|---|
| Local to Live | Local branch data is pushed to the central server |
| Live to Local | Central server data is pulled down to the branch |
| Bidirectional | Both directions, with conflict resolution applied |

**Conflict Resolution**

| Strategy | Behavior |
|---|---|
| Live Server Wins | Remote data always overwrites local |
| Local Server Wins | Local data always overwrites remote |
| Latest Timestamp Wins | Most recently modified record is kept |
| Manual Review | Conflicts are logged for a human to resolve |

## Troubleshooting

**Connection failed** — Verify the live server URL is reachable, and that the API credentials belong to a System Manager user.

**Sync errors** — Open **Sync Logs** to see which records failed and why. Common causes are missing DocTypes on one server or permission issues.

**Data mismatch after sync** — Run a selective sync on the affected module rather than a full sync, then check the logs.

## License

MIT — see [license.txt](license.txt)

Copyright (c) 2026 Plus Care Pharmacy — Yemen
