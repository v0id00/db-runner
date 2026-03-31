# db-runner

Broadcast SQL to multiple MySQL/MariaDB databases in parallel.

- Pick which databases to target via an interactive vim buffer
- Watch progress with a live progress bar and ETA
- Review results in vim — save the log in plain, JSON, or CSV format

## Requirements

- Python 3.11+
- `vim`
- MySQL/MariaDB servers

## Installation

```bash
pipx install git+https://github.com/your-username/db-runner.git
```

Or from a local clone:

```bash
git clone https://github.com/your-username/db-runner.git
cd db-runner
pipx install .
```

## Configuration

```bash
cp connections.example.json connections.json
# edit connections.json with your server details
```

`connections.json` format:

```json
[
  {
    "name": "prod-1",
    "host": "db1.example.com",
    "port": 3306,
    "user": "myuser",
    "password": "mypassword",
    "max_connections": 3
  }
]
```

| Field             | Required | Default       | Description                              |
|-------------------|----------|---------------|------------------------------------------|
| `host`            | ✓        |               | Server hostname or IP                    |
| `user`            | ✓        |               | MySQL username                           |
| `password`        | ✓        |               | MySQL password                           |
| `name`            |          | same as host  | Display name                             |
| `port`            |          | `3306`        | MySQL port                               |
| `max_connections` |          | `3`           | Max parallel connections to this server  |

## Usage

```bash
db-runner                          # vim opens for SQL input
db-runner --sql update.sql         # read SQL from file
db-runner --dry-run                # preview targeted databases without executing
db-runner -c /path/to/servers.json # use a different config file
```

### All options

```
  -c, --connections FILE   Connection config file (default: connections.json)
  --sql FILE               Read SQL from file instead of opening vim
  --dry-run                Show targeted databases without executing
  --force                  Skip confirmation for destructive SQL
  --timeout SECONDS        Query timeout in seconds (default: 30)
  --no-transaction         Run in autocommit mode instead of transactions
  --log-format FORMAT      Log format: plain (default), json, csv
  --failed-output FILE     Save failed server:db entries to this file
  --show-results           Include SELECT result rows in the log
```

## Workflow

1. **SQL input** — vim opens with your recent query history as comments
2. **Database list** — all non-system databases are fetched from every server
3. **Filter** — vim opens with a `server:db` list; delete lines you want to skip
4. **Execute** — SQL runs in parallel across all selected databases
5. **Progress** — live bar shows completed/total, success/error counts, and ETA
6. **Log** — vim shows the full result log; save it with `:w output.log`

## Safety

- **Destructive SQL detection**: queries containing `DROP`, `TRUNCATE`, `DELETE`, or `ALTER TABLE` trigger a confirmation prompt. Use `--force` to bypass.
- **Transactions**: each query runs inside a transaction by default; errors trigger automatic rollback. Use `--no-transaction` to disable.
- **Query history**: every executed SQL is stored in `~/.db_runner_history` (last 100 entries).

## License

MIT
