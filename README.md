# Plus Care Sync

Offline sync solution for ERPNext - sync data between local and live servers.

## Features

- **Bidirectional Sync** - Sync data from local to live, live to local, or both ways
- **Selective Sync** - Choose specific modules or DocTypes to sync
- **Conflict Resolution** - Multiple strategies to handle data conflicts
- **Automatic or Manual** - Schedule automatic syncs or trigger manually
- **Comprehensive Logging** - Track all sync operations and errors
- **Offline Support** - Queue changes when offline, sync when online

## Installation

```bash
cd /workspace/erpnext-dev/frappe-bench
bench --site erpnext.localhost install-app plus_care_sync
bench --site erpnext.localhost migrate
bench --site erpnext.localhost clear-cache
```

## Quick Start

1. Navigate to: **Setup → Sync Settings**
2. Enable sync
3. Configure sync direction and data scope
4. Enter remote server credentials
5. Test connection
6. Click "Sync Now"

## Configuration Guide

### Sync Direction Options

- **Local to Live (One Way)** - Local is source of truth
- **Live to Local (One Way)** - Live server is source
- **Bidirectional (Two Way)** - Both can create/edit data

### Data Scope Options

**Selective Modules** (Recommended):
- Sales, Purchase, Stock
- Accounting, Customers, Items
- HR & Payroll
- Custom DocTypes

**Full Database** - Sync everything (use with caution)

### Conflict Resolution

- **Live Server Wins** - Remote data takes priority
- **Local Server Wins** - Local data takes priority
- **Latest Timestamp Wins** - Most recent modification wins
- **Manual Review** - Log conflicts for manual resolution

## Remote Server Setup

1. On live server, create API keys:
   - Go to User → API Access
   - Generate Keys
   - Copy API Key and Secret

2. On local server, enter in Sync Settings:
   - Remote URL: `https://yourcompany.erpnext.com`
   - API Key: (from step 1)
   - API Secret: (from step 1)

## Usage

### Manual Sync
Click "Sync Now" button in Sync Settings

### Automatic Sync
1. Set Sync Mode to "Automatic"
2. Choose frequency (5 min to Daily)
3. Save settings

### View Logs
Click "View Sync Logs" to see sync history and errors

## Troubleshooting

**Connection Failed**
- Verify remote URL is accessible
- Check API credentials are correct
- Ensure System Manager role on remote

**Sync Errors**
- Check Sync Logs for details
- Verify DocTypes exist on both servers
- Check network connectivity

## Best Practices

1. Test with one module before full sync
2. Backup both servers before initial sync
3. Use selective modules for better performance
4. Schedule heavy syncs during off-hours
5. Monitor logs regularly

## Contributing

This app uses `pre-commit` for code formatting:

```bash
cd apps/plus_care_sync
pre-commit install
```

## License

MIT
