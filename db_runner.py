#!/usr/bin/env python3
"""
db-runner: Bulk SQL execution tool for MySQL/MariaDB databases

Usage:
  db-runner                          # enter SQL via vim
  db-runner -c /path/to/servers.json # use a different config file
  db-runner --sql query.sql          # read SQL from file
  db-runner --help                   # all options
"""

import argparse
import csv
import io
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import termios
import tty
from datetime import datetime
from typing import Optional

try:
    import aiomysql
except ImportError:
    print("Error: aiomysql module is required. Install with: pip install aiomysql", file=sys.stderr)
    sys.exit(1)

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

console = Console()

SYSTEM_DBS = frozenset({
    "information_schema",
    "mysql",
    "performance_schema",
    "sys",
    "innodb",
})

DESTRUCTIVE_KEYWORDS = (
    r"\bDROP\b",
    r"\bTRUNCATE\b",
    r"\bDELETE\b",
    r"\bALTER\s+TABLE\b",
)


def check_destructive(sql: str, force: bool = False) -> None:
    """
    Show a warning if SQL contains destructive keywords and prompt for confirmation.
    If `force=True`, confirmation is skipped.
    """
    found = [
        kw.replace(r"\b", "").replace(r"\s+", " ")
        for kw in DESTRUCTIVE_KEYWORDS
        if re.search(kw, sql, re.IGNORECASE)
    ]
    if not found:
        return

    console.print()
    console.print(Panel(
        "[bold red]⚠ Destructive SQL detected![/]\n\n"
        f"Detected keywords: [red]{', '.join(found)}[/]\n\n"
        "[dim]Confirm to continue, Ctrl+C to cancel[/]",
        border_style="red",
        title="[bold red]Warning[/]",
    ))

    if force:
        console.print("[yellow]Confirmation skipped via --force.[/]")
        return

    console.print("[bold]Type [red]YES[/] to continue:[/] ", end="")
    try:
        answer = input()
    except (EOFError, KeyboardInterrupt):
        console.print("\n[yellow]Cancelled.[/]")
        sys.exit(0)

    if answer.strip().upper() != "YES":
        console.print("[yellow]Cancelled.[/]")
        sys.exit(0)


# ---------------------------------------------------------------------------
# 1. Connection configuration
# ---------------------------------------------------------------------------

def load_connections(path: str) -> list[dict]:
    """Load and validate connections.json."""
    try:
        with open(path) as f:
            conns = json.load(f)
    except FileNotFoundError:
        console.print(f"[red]Error:[/] '{path}' not found.")
        sys.exit(1)
    except json.JSONDecodeError as e:
        console.print(f"[red]JSON parse error:[/] {e}")
        sys.exit(1)

    if not isinstance(conns, list) or len(conns) == 0:
        console.print("[red]Error:[/] connections.json is empty or not a list.")
        sys.exit(1)

    required = {"host", "user", "password"}
    for i, conn in enumerate(conns):
        missing = required - conn.keys()
        if missing:
            console.print(f"[red]Error:[/] Connection #{i} missing fields: {', '.join(missing)}")
            sys.exit(1)
        conn.setdefault("port", 3306)
        conn.setdefault("name", conn["host"])
        conn.setdefault("max_connections", 3)

    return conns


# ---------------------------------------------------------------------------
# 2. SQL history
# ---------------------------------------------------------------------------

HISTORY_FILE = os.path.expanduser("~/.db_runner_history")
HISTORY_MAX  = 100   # maximum number of entries to store
HISTORY_SHOW = 10    # last N entries shown in vim preview


def history_load() -> list[dict]:
    """Read ~/.db_runner_history as JSON lines."""
    if not os.path.exists(HISTORY_FILE):
        return []
    entries = []
    try:
        with open(HISTORY_FILE) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except OSError:
        pass
    return entries


def history_save(sql: str) -> None:
    """Append SQL to the history file, dropping oldest entries when HISTORY_MAX is exceeded."""
    entry = {"ts": datetime.now().isoformat(timespec="seconds"), "sql": sql}
    entries = history_load()
    entries.append(entry)
    entries = entries[-HISTORY_MAX:]
    try:
        with open(HISTORY_FILE, "w") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
    except OSError as exc:
        console.print(f"[yellow]Warning: history could not be saved:[/] {exc}")


def history_comment_block() -> str:
    """Prepend the last HISTORY_SHOW entries as comments to the vim template."""
    entries = history_load()
    if not entries:
        return ""
    recent = entries[-HISTORY_SHOW:][::-1]  # newest first
    lines = ["-- ──── Recent queries (history) ──────────────────────────────"]
    for e in recent:
        ts = e.get("ts", "")
        for i, sql_line in enumerate(e["sql"].splitlines()):
            prefix = f"-- [{ts}] " if i == 0 else "--          "
            lines.append(f"{prefix}{sql_line}")
    lines.append("-- ─────────────────────────────────────────────────────────")
    lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 3. Vim integration
# ---------------------------------------------------------------------------

def open_vim(content: str, suffix: str = ".txt", comment: str = "") -> str:
    """
    Open a temporary file in vim and return the saved content.
    Returns an empty string if vim exits with a non-zero code or content is unchanged.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=suffix, delete=False, prefix="db_runner_"
    ) as f:
        if comment:
            f.write(comment)
        f.write(content)
        tmp_path = f.name

    try:
        ret = subprocess.run(["vim", tmp_path])
        if ret.returncode != 0:
            return ""
        with open(tmp_path) as f:
            return f.read()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def get_sql_from_vim() -> str:
    """Get SQL input from the user via vim (appending history to the template)."""
    console.print("\n[bold cyan]► Opening vim[/] — write your SQL, save and quit [dim](:wq)[/]\n")

    history_block = history_comment_block()
    template = (
        history_block
        + "-- Write your SQL here\n"
        + "-- Use semicolons (;) for multiple statements\n"
        + "-- When ready: :wq\n\n"
    )
    content = open_vim(template, suffix=".sql")

    sql_lines = [
        line for line in content.splitlines()
        if line.strip() and not line.strip().startswith("--")
    ]
    sql = "\n".join(sql_lines).strip()

    if not sql:
        console.print("[yellow]SQL is empty, exiting.[/]")
        sys.exit(0)

    return sql


# ---------------------------------------------------------------------------
# 3. Fetching database lists
# ---------------------------------------------------------------------------

async def fetch_databases_for(conn: dict) -> tuple[str, list[str], Optional[str]]:
    """Fetch the database list from a single server."""
    name = conn["name"]
    try:
        connection = await aiomysql.connect(
            host=conn["host"],
            port=conn["port"],
            user=conn["user"],
            password=conn["password"],
            connect_timeout=10,
        )
        try:
            async with connection.cursor() as cursor:
                await cursor.execute("SHOW DATABASES")
                rows = await cursor.fetchall()
                dbs = [row[0] for row in rows if row[0].lower() not in SYSTEM_DBS]
                return name, dbs, None
        finally:
            connection.close()
    except Exception as e:
        return name, [], str(e)


async def fetch_all_databases(connections: list[dict]) -> dict[str, list[str]]:
    """Fetch database lists from all servers concurrently."""
    console.print("\n[bold]Fetching database lists...[/]")

    tasks = [fetch_databases_for(conn) for conn in connections]
    results = await asyncio.gather(*tasks)

    db_map: dict[str, list[str]] = {}
    for name, dbs, error in results:
        if error:
            console.print(f"  [red]✗ {name}:[/] {error}")
        else:
            console.print(f"  [green]✓ {name}:[/] {len(dbs)} database(s)")
            if dbs:
                db_map[name] = dbs

    return db_map


# ---------------------------------------------------------------------------
# 4. Database selection (filtering via vim)
# ---------------------------------------------------------------------------

def parse_server_db_line(line: str) -> Optional[tuple[str, str]]:
    """Parse a 'server_name.db_name' line; handles server names containing dots."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    dot = line.rfind(".")
    if dot < 1:
        return None
    server_name = line[:dot].strip()
    db_name = line[dot + 1:].strip()
    if server_name and db_name:
        return server_name, db_name
    return None


def select_databases(
    db_map: dict[str, list[str]],
    dbfilter: Optional[str] = None,
) -> list[tuple[str, str]]:
    """
    Display all DBs in vim; the user deletes lines they don't want to target.
    Returns a list of (server_name, db_name) pairs.
    """
    all_entries = []
    for server_name, dbs in db_map.items():
        for db in sorted(dbs):
            all_entries.append(f"{server_name}.{db}")

    if not all_entries:
        console.print("[red]No databases found, exiting.[/]")
        sys.exit(0)

    # Apply dbfilter
    if dbfilter:
        try:
            pattern = re.compile(dbfilter, re.IGNORECASE)
        except re.error as e:
            console.print(f"[red]Error:[/] --dbfilter regex is invalid: {e}")
            sys.exit(1)
        filtered_entries = [e for e in all_entries if pattern.search(e.split(".", 1)[-1] if "." in e else e)]
        filtered_out = len(all_entries) - len(filtered_entries)
        if filtered_out:
            console.print(f"[dim]--dbfilter '{dbfilter}': {filtered_out} database(s) filtered out.[/]")
        all_entries = filtered_entries

    if not all_entries:
        console.print("[red]No databases match the filter, exiting.[/]")
        sys.exit(0)

    filter_note = f"\n# --dbfilter '{dbfilter}' is active\n" if dbfilter else ""
    comment = (
        "# ──────────────────────────────────────────────────────────────\n"
        "# DELETE lines you do NOT want to target, or prefix them with #\n"
        "# Line format: server_name.database_name\n"
        f"# To select all, just save: :wq{filter_note}"
        "# ──────────────────────────────────────────────────────────────\n\n"
    )

    total = len(all_entries)
    console.print(
        f"\n[bold cyan]► Opening vim[/] — [bold]{total}[/] database(s) listed. "
        "Delete the ones you don't want to target.\n"
    )

    content = open_vim("\n".join(all_entries) + "\n", suffix=".txt", comment=comment)

    selected: list[tuple[str, str]] = []
    for line in content.splitlines():
        parsed = parse_server_db_line(line)
        if parsed:
            selected.append(parsed)

    return selected


# ---------------------------------------------------------------------------
# 5-6. Parallel SQL execution + progress bar
# ---------------------------------------------------------------------------

def wait_for_keypress(prompt: str = "\n[dim]Press any key to continue...[/]") -> None:
    """Wait for a single keypress in the terminal (no echo)."""
    console.print(prompt, end="")
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    console.print()


async def execute_on_db(
    conn: dict,
    db_name: str,
    sql: str,
    semaphore: asyncio.Semaphore,
    progress: Progress,
    task_id: object,
    results: list,
    dry_run: bool = False,
    timeout: int = 30,
    use_transaction: bool = True,
    show_results: bool = False,
) -> None:
    """Execute SQL on one database (rate-limited by semaphore)."""
    server_name = conn["name"]
    async with semaphore:
        if dry_run:
            await asyncio.sleep(0)
            results.append({
                "server": server_name,
                "db": db_name,
                "status": "DRY",
                "affected": 0,
                "error": None,
                "rows": None,
            })
            progress.advance(task_id)
            return

        try:
            connection = await aiomysql.connect(
                host=conn["host"],
                port=conn["port"],
                user=conn["user"],
                password=conn["password"],
                db=db_name,
                connect_timeout=timeout,
                autocommit=not use_transaction,
            )
            try:
                async with connection.cursor() as cursor:
                    await asyncio.wait_for(cursor.execute(sql), timeout=timeout)
                    affected = cursor.rowcount
                    rows = None
                    if show_results:
                        rows = await cursor.fetchall()
                        col_names = [d[0] for d in cursor.description] if cursor.description else []
                        rows = {"columns": col_names, "data": [list(row) for row in rows]}
                    if use_transaction:
                        await connection.commit()
                    results.append({
                        "server": server_name,
                        "db": db_name,
                        "status": "OK",
                        "affected": affected,
                        "error": None,
                        "rows": rows,
                    })
            except Exception:
                if use_transaction:
                    await connection.rollback()
                raise
            finally:
                connection.close()
        except Exception as e:
            results.append({
                "server": server_name,
                "db": db_name,
                "status": "ERR",
                "affected": 0,
                "error": str(e),
                "rows": None,
            })
        finally:
            progress.advance(task_id)


async def run_sql_on_all(
    selected: list[tuple[str, str]],
    connections: list[dict],
    sql: str,
    dry_run: bool = False,
    timeout: int = 30,
    use_transaction: bool = True,
    show_results: bool = False,
) -> list[dict]:
    """Send SQL to the selected databases in parallel."""
    conn_map = {conn["name"]: conn for conn in connections}

    semaphores: dict[str, asyncio.Semaphore] = {
        server: asyncio.Semaphore(conn_map[server].get("max_connections", 3))
        for server in {s for s, _ in selected}
        if server in conn_map
    }

    results: list[dict] = []

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=40),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        TextColumn("ETA:"),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    )
    dry_label = " [yellow](DRY RUN)[/]" if dry_run else ""
    progress.start()
    task_id = progress.add_task(f"[cyan]Sending SQL...{dry_label}", total=len(selected))

    tasks = []
    for server_name, db_name in selected:
        if server_name not in conn_map:
            results.append({
                "server": server_name,
                "db": db_name,
                "status": "ERR",
                "affected": 0,
                "error": f"Server not defined: {server_name}",
            })
            progress.advance(task_id)
            continue

        tasks.append(
            execute_on_db(
                conn_map[server_name],
                db_name,
                sql,
                semaphores[server_name],
                progress,
                task_id,
                results,
                dry_run=dry_run,
                timeout=timeout,
                use_transaction=use_transaction,
                show_results=show_results,
            )
        )

    await asyncio.gather(*tasks)

    dry_count = sum(1 for r in results if r["status"] == "DRY")
    ok = sum(1 for r in results if r["status"] == "OK")
    err = len(results) - ok - dry_count
    if dry_run:
        status_color = "yellow"
        done_label = f"[yellow]DRY RUN — {dry_count} database(s) targeted[/]"
    else:
        status_color = "green" if err == 0 else "yellow" if ok > 0 else "red"
        done_label = f"[{status_color}]✓ Done[/]  [green]{ok} successful[/]  [red]{err} failed[/]"
    progress.update(task_id, description=done_label)
    progress.stop()

    wait_for_keypress()

    return results


# ---------------------------------------------------------------------------
# 7. Log display
# ---------------------------------------------------------------------------

def format_results(
    results: list[dict],
    sql: str,
    log_format: str,
    dry_run: bool,
    timestamp: str,
) -> str:
    """Render results to a string in the requested format."""
    dry_count = sum(1 for r in results if r["status"] == "DRY")
    ok_count  = sum(1 for r in results if r["status"] == "OK")
    err_count = sum(1 for r in results if r["status"] == "ERR")
    sorted_results = sorted(results, key=lambda x: (x["status"] != "ERR", x["server"], x["db"]))

    if log_format == "json":
        payload = {
            "timestamp": timestamp,
            "dry_run": dry_run,
            "sql": sql,
            "summary": {"total": len(results), "ok": ok_count, "err": err_count, "dry": dry_count},
            "results": sorted_results,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"

    if log_format == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["timestamp", "server", "db", "status", "affected", "error"])
        for r in sorted_results:
            writer.writerow([timestamp, r["server"], r["db"], r["status"], r["affected"], r["error"] or ""])
        return buf.getvalue()

    # plain (default)
    dry_tag = "  [DRY RUN]" if dry_run else ""
    lines = [
        f"# db-runner Log — {timestamp}{dry_tag}",
        f"# Total: {len(results)}  Successful: {ok_count}  Failed: {err_count}"
        + (f"  DryRun: {dry_count}" if dry_run else ""),
        "#",
        "# Executed SQL:",
        *[f"#   {line}" for line in sql.splitlines()],
        "#",
        "# ─────────────────────────────────────────────────────────────",
        "# To save:  :w /full/path/file.log",
        "# To quit:  :q",
        "# ─────────────────────────────────────────────────────────────",
        "",
    ]
    for r in sorted_results:
        if r["status"] == "OK":
            lines.append(f"[OK]  {r['server']}:{r['db']}  affected={r['affected']}")
            if r.get("rows") and r["rows"]["data"]:
                cols = r["rows"]["columns"]
                lines.append(f"      columns: {', '.join(cols)}")
                for row_data in r["rows"]["data"]:
                    lines.append(f"      {row_data}")
        elif r["status"] == "DRY":
            lines.append(f"[DRY] {r['server']}:{r['db']}")
        else:
            lines.append(f"[ERR] {r['server']}:{r['db']}  {r['error']}")
    return "\n".join(lines) + "\n"


def show_log(
    results: list[dict],
    sql: str,
    dry_run: bool = False,
    log_format: str = "plain",
    failed_output: Optional[str] = None,
) -> None:
    """Display results in a summary panel and in vim; optionally save to file."""
    dry_count = sum(1 for r in results if r["status"] == "DRY")
    ok_count  = sum(1 for r in results if r["status"] == "OK")
    err_count = sum(1 for r in results if r["status"] == "ERR")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Print summary to terminal
    if dry_run:
        summary_color = "yellow"
        summary_text = f"[yellow]DRY RUN[/]  {dry_count} database(s) would be targeted"
    else:
        summary_color = "green" if err_count == 0 else "yellow" if ok_count > 0 else "red"
        summary_text = (
            f"[green]Successful:[/] {ok_count}   [red]Failed:[/] {err_count}   "
            f"[dim]Total: {len(results)}[/]"
        )
    console.print()
    console.print(Panel(
        summary_text,
        title="[bold]Result Summary[/]",
        border_style=summary_color,
    ))

    # Save failed DBs to a separate file
    failed_results = [r for r in results if r["status"] == "ERR"]
    if failed_output and failed_results:
        try:
            with open(failed_output, "w") as f:
                for r in failed_results:
                    f.write(f"{r['server']}:{r['db']}\n")
            console.print(f"[yellow]⚠[/] {len(failed_results)} failed DB(s) written to '{failed_output}'.")
        except OSError as exc:
            console.print(f"[red]Error: could not write failed-output:[/] {exc}")

    log_content = format_results(results, sql, "plain", dry_run, timestamp)

    console.print("\n[bold cyan]► Opening vim[/] — viewing log [dim](:w file.log to save, :q to quit)[/]\n")

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".log", delete=False, prefix="db_runner_log_"
    ) as f:
        f.write(log_content)
        tmp_path = f.name

    try:
        subprocess.run(["vim", tmp_path])
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

HELP_TEXT = """[bold cyan]db-runner[/] — Bulk SQL execution tool for MySQL/MariaDB databases

[bold]USAGE[/]
  [cyan]db-runner[/] [options]

[bold]OPTIONS[/]
  [green]-c, --connections[/] [dim]FILE[/]    Connection configuration file (default: [dim]connections.json[/])
  [green]--sql[/] [dim]FILE[/]                Read SQL from file (opens vim if omitted)
  [green]--dry-run[/]                   Show target DBs without executing SQL
  [green]--force[/]                     Skip confirmation for destructive SQL
  [green]--timeout[/] [dim]SECONDS[/]         Query timeout in seconds (default: [dim]30[/])
  [green]--no-transaction[/]            Run in autocommit mode (no transaction)
  [green]--log-format[/] [dim]FORMAT[/]        Log format: [dim]plain[/] (default) [dim]| json | csv[/]
  [green]--failed-output[/] [dim]FILE[/]      Save failed DBs in [dim]server:db[/] format to this file
  [green]--show-results[/]              Show SELECT result rows in the log
  [green]-h, --help[/]                  Show this help page

[bold]EXAMPLES[/]
  [dim]# Standard usage — enter SQL via vim, filter the DB list[/]
  [cyan]db-runner[/]

  [dim]# Read SQL from file[/]
  [cyan]db-runner[/] --sql update.sql

  [dim]# Preview which DBs would be affected (does not execute)[/]
  [cyan]db-runner[/] --dry-run --sql fix.sql

  [dim]# Send a DELETE query without confirmation, 60s timeout[/]
  [cyan]db-runner[/] --sql cleanup.sql --force --timeout 60

  [dim]# Save SELECT results to a JSON log[/]
  [cyan]db-runner[/] --sql report.sql --show-results --log-format json

  [dim]# Different server config, write failures to a separate file[/]
  [cyan]db-runner[/] -c /etc/servers.json --failed-output retry.txt

[bold]WORKFLOW[/]
  [dim]1.[/] [cyan]connections.json[/] is read (cp connections.example.json connections.json)
  [dim]2.[/] vim opens → write SQL [dim](:wq)[/]  [dim]Recent queries appear as comment lines[/]
  [dim]3.[/] DB lists are fetched from servers [dim](SHOW DATABASES)[/]
  [dim]4.[/] vim opens → [cyan]server:db[/] list, delete lines you don't want
  [dim]5.[/] SQL is sent in parallel (per-server Semaphore)[/]
  [dim]6.[/] Progress bar + ETA shown, waits for a keypress when done
  [dim]7.[/] vim log buffer opens → save with [dim]:w file.log[/]

[bold]CONFIGURATION[/]
  [cyan]connections.json[/] format:
  [dim]{
    "name": "prod-1",        ← display name (optional, default: host)
    "host": "db.example.com",
    "port": 3306,            ← optional, default: 3306
    "user": "myuser",
    "password": "mypass",
    "max_connections": 3     ← optional, default: 3
  }[/]

[bold]HISTORY[/]
  Every executed SQL is saved to [cyan]~/.db_runner_history[/] (last 100 queries).
"""


def print_help() -> None:
    """Display the help page with rich formatting."""
    from rich.padding import Padding
    console.print(Padding(HELP_TEXT.strip(), (1, 2)))


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="db-runner",
        description="db-runner: Bulk SQL execution tool for MySQL/MariaDB",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    parser.add_argument(
        "-c", "--connections",
        default="connections.json",
        metavar="FILE",
        help="Connection configuration file (default: connections.json)",
    )
    parser.add_argument(
        "--sql",
        metavar="FILE",
        help="Read SQL from file (opens vim if not specified)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show target databases without executing SQL",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip confirmation for destructive SQL (DROP/TRUNCATE/DELETE/ALTER TABLE)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        metavar="SECONDS",
        help="Query timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--no-transaction",
        action="store_true",
        help="Run in autocommit mode (no transaction)",
    )
    parser.add_argument(
        "--log-format",
        choices=["plain", "json", "csv"],
        default="plain",
        help="Log output format: plain (default), json, csv",
    )
    parser.add_argument(
        "--failed-output",
        metavar="FILE",
        help="Save failed DBs in server:db format to this file",
    )
    parser.add_argument(
        "--show-results",
        action="store_true",
        help="Show rows returned by SELECT queries in the log",
    )
    parser.add_argument(
        "--dbfilter",
        metavar="REGEX",
        help="Regex filter for database names (applied before showing the list)",
    )
    parser.add_argument(
        "-h", "--help",
        action="store_true",
        help="Show this help page",
    )
    args = parser.parse_args()

    if args.help:
        print_help()
        sys.exit(0)

    dry_run: bool = args.dry_run

    header_extra = "  [yellow bold][DRY RUN][/]" if dry_run else ""
    console.print(Panel(
        f"[bold cyan]db-runner[/]  —  MySQL/MariaDB Bulk SQL Tool{header_extra}\n"
        "[dim]You can press Ctrl+C at any time to exit[/]",
        border_style="cyan",
    ))

    # 1. Load connections
    connections = load_connections(args.connections)
    console.print(f"[green]✓[/] {len(connections)} server connection(s) loaded.")

    # 2. Get SQL
    if args.sql:
        try:
            with open(args.sql) as f:
                sql = f.read().strip()
        except FileNotFoundError:
            console.print(f"[red]Error:[/] SQL file not found: {args.sql}")
            sys.exit(1)
        if not sql:
            console.print("[red]Error:[/] SQL file is empty.")
            sys.exit(1)
        console.print(f"[green]✓[/] SQL read from file: {args.sql}")
    else:
        sql = get_sql_from_vim()
        console.print(f"[green]✓[/] SQL received ({len(sql.splitlines())} line(s)).")

    history_save(sql)

    # 2b. Destructive keyword check
    check_destructive(sql, force=args.force)

    # 3. Fetch database lists
    try:
        db_map = asyncio.run(fetch_all_databases(connections))
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/]")
        sys.exit(0)

    if not db_map:
        console.print("[red]Could not connect to any server, exiting.[/]")
        sys.exit(1)

    # 4. Filter database list
    selected = select_databases(db_map, dbfilter=args.dbfilter)

    if not selected:
        console.print("[yellow]No databases selected, exiting.[/]")
        sys.exit(0)

    console.print(f"[green]✓[/] {len(selected)} database(s) selected.\n")

    # 5-6. Execute SQL in parallel + progress
    try:
        results = asyncio.run(run_sql_on_all(
            selected, connections, sql,
            dry_run=dry_run,
            timeout=args.timeout,
            use_transaction=not args.no_transaction,
            show_results=args.show_results,
        ))
    except KeyboardInterrupt:
        console.print("\n[yellow]Operation interrupted.[/]")
        sys.exit(0)

    # 7. Show log
    show_log(results, sql, dry_run=dry_run, log_format=args.log_format, failed_output=args.failed_output)


if __name__ == "__main__":
    main()
