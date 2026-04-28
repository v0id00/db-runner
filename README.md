# db-runner

Broadcast SQL to multiple MySQL/MariaDB databases in parallel.

- Pick which databases to target via an interactive vim buffer
- Filter servers and databases with regex and tags
- Watch progress with a live progress bar and ETA
- Review results in vim — save the log in plain, JSON, or CSV format
- Fully non-interactive mode for CI/cron pipelines

## Requirements

- Python 3.11+
- Linux or macOS (Windows is not supported — relies on POSIX terminal APIs)
- A terminal text editor (`vim`, `nano`, `hx`, etc.)
- MySQL/MariaDB servers

## Installation

```bash
pipx install git+https://github.com/v0id00/db-runner.git
```

Or from a local clone:

```bash
git clone https://github.com/v0id00/db-runner.git
cd db-runner
pipx install .
```

## Configuration

db-runner searches for the connections file in this order:

1. `./connections.json` — current working directory
2. `~/.config/db-runner/connections.json` — user config directory
3. An explicit path via `-c FILE`

Quick start:

```bash
# Option A: project-local file
cp connections.example.json connections.json

# Option B: user-wide config (available from any directory)
mkdir -p ~/.config/db-runner
cp connections.example.json ~/.config/db-runner/connections.json
```

`connections.json` is an array of server objects:

```json
[
  {
    "name": "prod-eu-1",
    "host": "db1.example.com",
    "port": 3306,
    "user": "myuser",
    "password": "mypassword",
    "max_connections": 3,
    "tags": ["prod", "eu"]
  }
]
```

| Field             | Required | Default      | Description                                    |
|-------------------|----------|--------------|------------------------------------------------|
| `host`            | ✓        |              | Server hostname or IP                          |
| `user`            | ✓        |              | MySQL username                                 |
| `password`        | ✓        |              | MySQL password (can be overridden by `--vault`)|
| `name`            |          | same as host | Display name / alias                           |
| `port`            |          | `3306`       | MySQL port                                     |
| `max_connections` |          | `3`          | Max parallel connections to this server        |
| `tags`            |          | `[]`         | Tag list for use with `--tags`                 |

## Usage

```bash
db-runner                              # editor opens for SQL input
db-runner --wizard                     # interactive setup wizard
db-runner --sql update.sql             # read SQL from file
db-runner --sql a.sql b.sql c.sql      # run multiple files sequentially
db-runner --dry-run                    # preview targeted databases without executing
db-runner -c /path/to/servers.json     # use a different config file
```

### All options

| Option | Default | Description |
|--------|---------|-------------|
| `-c, --connections FILE` | auto | Connection config file |
| `--sql FILE [FILE ...]` | — | Read SQL from file(s); multiple files run sequentially |
| `--dry-run` | off | Preview targeted databases without executing |
| `--force` | off | Skip confirmation for destructive SQL |
| `--timeout SECONDS` | `30` | Per-query timeout |
| `--no-transaction` | off | Run in autocommit mode (no rollback on error) |
| `--log-format FORMAT` | `plain` | Log format: `plain`, `json`, or `csv` |
| `--output FILE` | — | Save log to file (format set by `--log-format`) |
| `--failed-output FILE` | — | Save failed `server.db` entries to a separate file |
| `--show-results` | off | Include SELECT result rows as formatted tables in the log |
| `--dbfilter REGEX` | — | Include only databases whose name matches this regex |
| `--exclude-db REGEX` | — | Exclude databases whose name matches this regex |
| `--server REGEX` | — | Filter connections by name/alias |
| `--tags TAG1,TAG2` | — | Filter connections by tags (comma-separated; any match) |
| `--stop-on-error` | off | Halt all execution on the first failure |
| `--retry N` | `0` | Retry failed databases N times with exponential backoff |
| `--delay MS` | `0` | Per-database delay in milliseconds (rate limiting) |
| `--concurrency N` | per-server | Override `max_connections` globally for this run |
| `--delimiter STR` | `;` | Statement separator (e.g. `$$` for stored procedures) |
| `--quiet` | off | No progress bar, no keypress, no editor log (CI/cron mode) |
| `--no-vim` | off | Skip all editor steps; read SQL from stdin if `--sql` not given |
| `--vault FILE` | — | Load passwords from a `name=password` file |
| `--no-partial-log` | off | On Ctrl+C, exit silently instead of showing partial results |
| `--wizard` | off | Launch interactive setup wizard to configure all options |
| `-h, --help` | — | Show the help page |

## Workflow

1. **SQL input** — editor opens with your recent query history as comments
2. **Database list** — all non-system databases are fetched from every server
3. **Filter** — editor opens with a `server.db` list; delete lines you want to skip
4. **Execute** — SQL runs in parallel across all selected databases
5. **Progress** — live bar shows completed/total, success/error counts, and ETA
6. **Log** — editor shows the full result log; save it with `:w output.log`

## Editor

db-runner opens the editor in this order:

1. `$VISUAL` environment variable
2. `$EDITOR` environment variable
3. `vim` (fallback)

```bash
# Use nano
EDITOR=nano db-runner

# Or export permanently in your shell profile
export VISUAL=hx
```

## Filtering

Databases are displayed in `server_name.database_name` format during the vim selection step.

```bash
# Only target databases whose name starts with "shop_"
db-runner --dbfilter "^shop_"

# Exclude any database named like a test/dev environment
db-runner --exclude-db "_(dev|test|staging)$"

# Only connect to servers tagged "prod" and "eu"
db-runner --tags prod,eu

# Only connect to servers whose name matches a regex
db-runner --server "prod-eu-[12]"
```

All filters can be combined — they are applied in order before the editor selection step opens.

## Wizard

`--wizard` launches an interactive step-by-step prompt that walks through every option:

```bash
db-runner --wizard
```

The wizard covers all options (connections file, SQL source, database filters, execution settings, output format) and prints the equivalent command-line invocation at the end before running. You can also combine `--wizard` with explicit flags — wizard values are applied first, explicit flags take precedence.

## Interrupt & partial log

If you press **Ctrl+C** while SQL is executing, db-runner stops cleanly and shows the log of all operations that completed before the interrupt. Use `--no-partial-log` to suppress this and exit immediately.

## Vault file

Keep `connections.json` password-free and load passwords from a separate secrets file:

```
# ~/.db_vault
prod-eu-1=secretpassword
prod-eu-2=anotherpassword
```

```bash
db-runner --vault ~/.db_vault
```

## CI / Non-interactive mode

```bash
# Fully non-interactive: read SQL from stdin, skip all editor steps
echo "UPDATE config SET value='1' WHERE key='flag'" | \
  db-runner --no-vim --quiet --log-format csv --output run.csv

# With file input and retries
db-runner --sql patch.sql --no-vim --quiet --retry 3 --stop-on-error
```

## Safety

- **Destructive SQL detection** — queries containing `DROP`, `TRUNCATE`, `DELETE`, or `ALTER TABLE` trigger a confirmation prompt. Use `--force` to bypass.
- **Transactions** — each query runs inside a transaction by default; errors trigger automatic rollback. Use `--no-transaction` to disable.
- **Dry run** — `--dry-run` shows exactly which databases would be targeted without executing anything.
- **Query history** — every executed SQL is saved to `~/.db_runner_history` (last 100 entries), shown as comments in the editor SQL buffer.

## License

MIT
