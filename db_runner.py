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

from rich import box
from rich.align import Align
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
from rich.rule import Rule
from rich.table import Table

console = Console()


def step_rule(label: str) -> None:
    """Print a labelled visual section separator."""
    console.print(Rule(f"[bold cyan] {label} [/]", style="dim cyan"))


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

def load_vault(path: str) -> dict[str, str]:
    """Load password vault from a key=value file."""
    vault: dict[str, str] = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and "=" in line:
                    key, _, value = line.partition("=")
                    vault[key.strip()] = value.strip()
    except FileNotFoundError:
        console.print(f"[red]Error:[/] vault file not found: {path}")
        sys.exit(1)
    except OSError as exc:
        console.print(f"[red]Error:[/] could not read vault file: {exc}")
        sys.exit(1)
    return vault


DEFAULT_CONFIG_PATHS = [
    "connections.json",
    os.path.expanduser("~/.config/db-runner/connections.json"),
]


def find_connections_file(explicit_path: Optional[str] = None) -> str:
    """
    Return the connections file path to use.
    If explicitly given via -c, use that (and error if missing).
    Otherwise search DEFAULT_CONFIG_PATHS in order.
    """
    if explicit_path:
        return explicit_path
    for candidate in DEFAULT_CONFIG_PATHS:
        if os.path.exists(candidate):
            return candidate
    # Nothing found — return the first candidate so load_connections()
    # can emit a meaningful 'file not found' error.
    return DEFAULT_CONFIG_PATHS[0]


def load_connections(path: Optional[str] = None, vault_path: Optional[str] = None) -> list[dict]:
    """Load and validate connections file, searching default locations if path is None."""
    resolved = find_connections_file(path)
    try:
        with open(resolved) as f:
            conns = json.load(f)
    except FileNotFoundError:
        searched = "\n  ".join(DEFAULT_CONFIG_PATHS)
        console.print(
            f"[red]Error:[/] connections file not found. Searched:\n  {searched}\n"
            "Run [cyan]cp connections.example.json connections.json[/] or use [cyan]-c FILE[/]."
        )
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
        conn.setdefault("tags", [])

    if vault_path:
        vault = load_vault(vault_path)
        for conn in conns:
            if conn["name"] in vault:
                conn["password"] = vault[conn["name"]]

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

def get_editor() -> str:
    """Return the user's preferred editor from $VISUAL or $EDITOR, falling back to vim."""
    return os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vim"


def open_editor(content: str, suffix: str = ".txt", comment: str = "") -> str:
    """
    Open a temporary file in the user's preferred editor and return the saved content.
    Returns an empty string if the editor exits with a non-zero code.
    """
    editor = get_editor()
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=suffix, delete=False, prefix="db_runner_"
    ) as f:
        if comment:
            f.write(comment)
        f.write(content)
        tmp_path = f.name

    try:
        ret = subprocess.run([editor, tmp_path])
        if ret.returncode != 0:
            return ""
        with open(tmp_path) as f:
            return f.read()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def get_sql_from_vim(no_vim: bool = False) -> str:
    """Get SQL input from the user via the preferred editor (appending history to the template)."""
    if no_vim:
        console.print("\n[bold cyan]Reading SQL from stdin...[/] (Ctrl+D to finish)\n")
        sql = sys.stdin.read().strip()
        if not sql:
            console.print("[yellow]SQL is empty, exiting.[/]")
            sys.exit(0)
        return sql

    editor = get_editor()
    console.print(f"\n[bold cyan]► Opening {editor}[/] — write your SQL, save and quit [dim](:wq)[/]\n")

    history_block = history_comment_block()
    template = (
        history_block
        + "-- Write your SQL here\n"
        + "-- Use semicolons (;) for multiple statements\n"
        + "-- When ready: :wq\n\n"
    )
    content = open_editor(template, suffix=".sql")

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
    tasks = [fetch_databases_for(conn) for conn in connections]
    fetch_results = await asyncio.gather(*tasks)

    table = Table(box=box.SIMPLE, show_header=True, header_style="dim", pad_edge=False)
    table.add_column("", width=2, no_wrap=True)
    table.add_column("Server", style="bold")
    table.add_column("Databases", justify="right", style="cyan")
    table.add_column("Info", style="dim")

    db_map: dict[str, list[str]] = {}
    for name, dbs, error in fetch_results:
        if error:
            table.add_row("[red]✗[/]", f"[red]{name}[/]", "─", error)
        else:
            table.add_row("[green]✓[/]", name, str(len(dbs)), "")
            if dbs:
                db_map[name] = dbs

    console.print(table)
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
    exclude_db: Optional[str] = None,
    no_vim: bool = False,
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

    # Apply dbfilter (include only matching)
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

    # Apply exclude_db (remove matching)
    if exclude_db:
        try:
            excl_pattern = re.compile(exclude_db, re.IGNORECASE)
        except re.error as e:
            console.print(f"[red]Error:[/] --exclude-db regex is invalid: {e}")
            sys.exit(1)
        before = len(all_entries)
        all_entries = [e for e in all_entries if not excl_pattern.search(e.split(".", 1)[-1] if "." in e else e)]
        excluded = before - len(all_entries)
        if excluded:
            console.print(f"[dim]--exclude-db '{exclude_db}': {excluded} database(s) excluded.[/]")

    if not all_entries:
        console.print("[red]No databases match the filter, exiting.[/]")
        sys.exit(0)

    if no_vim:
        selected: list[tuple[str, str]] = []
        for line in all_entries:
            parsed = parse_server_db_line(line)
            if parsed:
                selected.append(parsed)
        return selected

    filter_note = f"\n# --dbfilter '{dbfilter}' is active\n" if dbfilter else ""
    comment = (
        "# ──────────────────────────────────────────────────────────────\n"
        "# DELETE lines you do NOT want to target, or prefix them with #\n"
        "# Line format: server_name.database_name\n"
        f"# To select all, just save: :wq{filter_note}"
        "# ──────────────────────────────────────────────────────────────\n\n"
    )

    total = len(all_entries)
    editor = get_editor()
    console.print(
        f"\n[bold cyan]► Opening {editor}[/] — [bold]{total}[/] database(s) listed. "
        "Delete the ones you don't want to target.\n"
    )

    content = open_editor("\n".join(all_entries) + "\n", suffix=".txt", comment=comment)

    selected = []
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
    results: list,
    progress: Optional[Progress] = None,
    task_id: object = None,
    dry_run: bool = False,
    timeout: int = 30,
    use_transaction: bool = True,
    show_results: bool = False,
    stop_event: Optional[asyncio.Event] = None,
    retry: int = 0,
    delay_ms: int = 0,
    delimiter: str = ";",
) -> None:
    """Execute SQL on one database (rate-limited by semaphore)."""
    server_name = conn["name"]
    async with semaphore:
        if stop_event and stop_event.is_set():
            if progress is not None:
                progress.advance(task_id)
            return

        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000)

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
            if progress is not None:
                progress.advance(task_id)
            return

        last_error: Optional[Exception] = None
        for attempt in range(max(1, retry + 1)):
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
                        statements = [s.strip() for s in sql.split(delimiter) if s.strip()]
                        affected = 0
                        for stmt in statements:
                            await asyncio.wait_for(cursor.execute(stmt), timeout=timeout)
                            if cursor.rowcount > 0:
                                affected += cursor.rowcount
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
                last_error = None
                break
            except Exception as e:
                last_error = e
                if attempt < retry:
                    await asyncio.sleep(1.5 ** attempt)

        if last_error is not None:
            if stop_event:
                stop_event.set()
            results.append({
                "server": server_name,
                "db": db_name,
                "status": "ERR",
                "affected": 0,
                "error": str(last_error),
                "rows": None,
            })
        if progress is not None:
            progress.advance(task_id)


async def run_sql_on_all(
    selected: list[tuple[str, str]],
    connections: list[dict],
    sql: str,
    dry_run: bool = False,
    timeout: int = 30,
    use_transaction: bool = True,
    show_results: bool = False,
    stop_on_error: bool = False,
    retry: int = 0,
    delay_ms: int = 0,
    concurrency: Optional[int] = None,
    delimiter: str = ";",
    quiet: bool = False,
    _results: Optional[list[dict]] = None,
) -> list[dict]:
    """Send SQL to the selected databases in parallel."""
    # Use a pre-allocated list when provided so callers can read partial
    # results after a KeyboardInterrupt without losing completed work.
    results: list[dict] = _results if _results is not None else []

    conn_map = {conn["name"]: conn for conn in connections}

    if concurrency:
        semaphores: dict[str, asyncio.Semaphore] = {
            server: asyncio.Semaphore(concurrency)
            for server in {s for s, _ in selected}
            if server in conn_map
        }
    else:
        semaphores = {
            server: asyncio.Semaphore(conn_map[server].get("max_connections", 3))
            for server in {s for s, _ in selected}
            if server in conn_map
        }

    stop_event = asyncio.Event() if stop_on_error else None

    if quiet:
        progress = None
        task_id = None
    else:
        progress = Progress(
            SpinnerColumn(spinner_name="dots"),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=None, complete_style="cyan", finished_style="green"),
            MofNCompleteColumn(),
            TaskProgressColumn(),
            TextColumn("[dim]·[/]"),
            TimeElapsedColumn(),
            TextColumn("[dim]ETA[/]"),
            TimeRemainingColumn(),
            console=console,
            transient=False,
            expand=True,
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
            if progress is not None:
                progress.advance(task_id)
            continue

        tasks.append(
            execute_on_db(
                conn_map[server_name],
                db_name,
                sql,
                semaphores[server_name],
                results,
                progress=progress,
                task_id=task_id,
                dry_run=dry_run,
                timeout=timeout,
                use_transaction=use_transaction,
                show_results=show_results,
                stop_event=stop_event,
                retry=retry,
                delay_ms=delay_ms,
                delimiter=delimiter,
            )
        )

    interrupted = False
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        interrupted = True

    if progress is not None:
        dry_count = sum(1 for r in results if r["status"] == "DRY")
        ok = sum(1 for r in results if r["status"] == "OK")
        err = len(results) - ok - dry_count
        pending = len(selected) - len(results)
        if interrupted:
            done_label = (
                f"[yellow]⚠ Interrupted[/]  "
                f"[green]{ok} done[/]  [red]{err} failed[/]  [dim]{pending} pending[/]"
            )
        elif dry_run:
            done_label = f"[yellow]DRY RUN — {dry_count} database(s) targeted[/]"
        else:
            status_color = "green" if err == 0 else "yellow" if ok > 0 else "red"
            done_label = f"[{status_color}]✓ Done[/]  [green]{ok} successful[/]  [red]{err} failed[/]"
        progress.update(task_id, description=done_label)
        progress.stop()
        if interrupted:
            console.print(Rule(
                f"[yellow]⚠  Interrupted — {len(results)}/{len(selected)} completed[/]",
                style="yellow",
            ))
        elif dry_run:
            console.print(Rule("[yellow]Dry run complete[/]", style="yellow"))
        elif err == 0:
            console.print(Rule("[green]✓  All operations successful[/]", style="green"))
        else:
            console.print(Rule(f"[yellow]Done  ·  {ok} succeeded  ·  {err} failed[/]", style="yellow"))
        if not interrupted:
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
    sql_file_label: Optional[str] = None,
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
            "sql_file": sql_file_label,
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
    file_tag = f"  [{sql_file_label}]" if sql_file_label else ""
    lines = [
        f"# db-runner Log — {timestamp}{dry_tag}{file_tag}",
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
            lines.append(f"[OK]  {r['server']}.{r['db']}  affected={r['affected']}")
            if r.get("rows") and r["rows"]["data"]:
                cols = r["rows"]["columns"]
                col_widths = [max(len(str(c)), max((len(str(row[i])) for row in r["rows"]["data"]), default=0))
                              for i, c in enumerate(cols)]
                sep = "  ├─" + "─┬─".join("─" * w for w in col_widths) + "─┤"
                header = "  │ " + " │ ".join(str(c).ljust(col_widths[i]) for i, c in enumerate(cols)) + " │"
                divider = "  ├─" + "─┼─".join("─" * w for w in col_widths) + "─┤"
                top = "  ┌─" + "─┬─".join("─" * w for w in col_widths) + "─┐"
                bot = "  └─" + "─┴─".join("─" * w for w in col_widths) + "─┘"
                lines.append(top)
                lines.append(header)
                lines.append(divider)
                for row_data in r["rows"]["data"]:
                    lines.append("  │ " + " │ ".join(str(v).ljust(col_widths[i]) for i, v in enumerate(row_data)) + " │")
                lines.append(bot)
                lines.append(f"  ({len(r['rows']['data'])} row(s))")
            lines.append("")
        elif r["status"] == "DRY":
            lines.append(f"[DRY] {r['server']}.{r['db']}")
            lines.append("")
        else:
            lines.append(f"[ERR] {r['server']}.{r['db']}")
            lines.append(f"      {r['error']}")
            lines.append("")
    # Remove trailing blank line
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def show_log(
    results: list[dict],
    sql: str,
    dry_run: bool = False,
    log_format: str = "plain",
    failed_output: Optional[str] = None,
    sql_file_label: Optional[str] = None,
    quiet: bool = False,
    output_file: Optional[str] = None,
    no_vim: bool = False,
    interrupted: bool = False,
) -> None:
    """Display results in a summary panel and in editor; optionally save to file."""
    dry_count = sum(1 for r in results if r["status"] == "DRY")
    ok_count  = sum(1 for r in results if r["status"] == "OK")
    err_count = sum(1 for r in results if r["status"] == "ERR")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Print summary to terminal
    if interrupted:
        summary_color = "yellow"
    elif dry_run:
        summary_color = "yellow"
    else:
        summary_color = "green" if err_count == 0 else "yellow" if ok_count > 0 else "red"
    title_extra = f" — {sql_file_label}" if sql_file_label else ""
    title_interrupt = " (partial)" if interrupted else ""

    stats = Table(box=box.SIMPLE, show_header=False, pad_edge=False, padding=(0, 2))
    stats.add_column("metric")
    stats.add_column("count", justify="right", min_width=4)
    if dry_run:
        stats.add_row("[yellow]◆  Dry run targets[/]", f"[bold yellow]{dry_count}[/]")
    else:
        if interrupted:
            stats.add_row("[yellow]⚠  Interrupted (partial)[/]", "")
        stats.add_row("[green]✓  Successful[/]", f"[bold green]{ok_count}[/]")
        stats.add_row(
            "[red]✗  Failed[/]" if err_count else "[dim]✗  Failed[/]",
            f"[bold red]{err_count}[/]" if err_count else f"[dim]{err_count}[/]",
        )
        stats.add_row("[dim]   Total[/]", f"[dim]{len(results)}[/]")
    console.print()
    console.print(Panel(
        stats,
        title=f"[bold]Result Summary{title_extra}{title_interrupt}[/]",
        border_style=summary_color,
        expand=False,
        padding=(0, 2),
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

    # Save log to --output file if specified
    if output_file:
        try:
            with open(output_file, "w") as f:
                f.write(format_results(results, sql, log_format, dry_run, timestamp, sql_file_label=sql_file_label))
            console.print(f"[green]✓[/] Log saved to {output_file}")
        except OSError as exc:
            console.print(f"[red]Error: could not write output file:[/] {exc}")

    if quiet or no_vim:
        return

    log_content = format_results(results, sql, "plain", dry_run, timestamp, sql_file_label=sql_file_label)

    editor = get_editor()
    console.print(f"\n[bold cyan]► Opening {editor}[/] — viewing log [dim](:w file.log to save, :q to quit)[/]\n")

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".log", delete=False, prefix="db_runner_log_"
    ) as f:
        f.write(log_content)
        tmp_path = f.name

    try:
        subprocess.run([editor, tmp_path])
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
  [green]-c, --connections[/] [dim]FILE[/]       Connection config file
                             [dim]Default search order:[/]
                               [dim]1. ./connections.json[/]
                               [dim]2. ~/.config/db-runner/connections.json[/]
  [green]--sql[/] [dim]FILE [[dim]FILE ...[/]][/]       Read SQL from file(s) (opens editor if omitted; multiple = sequential)
  [green]--dry-run[/]                      Show target DBs without executing SQL
  [green]--force[/]                        Skip confirmation for destructive SQL
  [green]--timeout[/] [dim]SECONDS[/]            Query timeout in seconds (default: [dim]30[/])
  [green]--no-transaction[/]               Run in autocommit mode (no transaction)
  [green]--log-format[/] [dim]FORMAT[/]           Log format: [dim]plain[/] (default) [dim]| json | csv[/]
  [green]--output[/] [dim]FILE[/]                Save log to file (format controlled by [dim]--log-format[/])
  [green]--failed-output[/] [dim]FILE[/]         Save failed DBs in [dim]server:db[/] format to this file
  [green]--show-results[/]                 Show SELECT result rows in the log
  [green]--dbfilter[/] [dim]REGEX[/]             Include only databases matching this regex
  [green]--exclude-db[/] [dim]REGEX[/]           Exclude databases matching this regex
  [green]--server[/] [dim]REGEX[/]               Filter connections by name/alias
  [green]--tags[/] [dim]TAG1,TAG2[/]             Filter connections by tags (any match)
  [green]--stop-on-error[/]                Halt all execution on first failure
  [green]--retry[/] [dim]N[/]                   Retry failed databases N times (exponential backoff)
  [green]--delay[/] [dim]MS[/]                  Per-database delay in milliseconds (rate limiting)
  [green]--concurrency[/] [dim]N[/]              Override per-server max_connections globally
  [green]--delimiter[/] [dim]STR[/]              Statement separator (default: [dim];[/])
  [green]--quiet[/]                        Suppress progress bar, keypress, and editor log (CI/cron)
  [green]--no-vim[/]                       Skip all editor steps; SQL from stdin if [dim]--sql[/] not given
  [green]--vault[/] [dim]FILE[/]                Key=value file to override connection passwords
  [green]--no-partial-log[/]               On Ctrl+C, exit silently instead of showing completed results
  [green]--wizard[/]                       Launch interactive setup wizard to configure all options
  [green]-h, --help[/]                     Show this help page

[bold]EXAMPLES[/]
  [dim]# Interactive wizard — configure everything via prompts[/]
  [cyan]db-runner[/] --wizard

  [dim]# Standard usage — enter SQL via editor, filter the DB list[/]
  [cyan]db-runner[/]

  [dim]# Read SQL from file[/]
  [cyan]db-runner[/] --sql update.sql

  [dim]# Run multiple SQL files sequentially[/]
  [cyan]db-runner[/] --sql step1.sql step2.sql step3.sql

  [dim]# Preview which DBs would be affected (does not execute)[/]
  [cyan]db-runner[/] --dry-run --sql fix.sql

  [dim]# Send a DELETE query without confirmation, 60s timeout[/]
  [cyan]db-runner[/] --sql cleanup.sql --force --timeout 60

  [dim]# Save SELECT results to a JSON log file[/]
  [cyan]db-runner[/] --sql report.sql --show-results --log-format json --output report.json

  [dim]# Different server config, write failures to a separate file[/]
  [cyan]db-runner[/] -c /etc/servers.json --failed-output retry.txt

  [dim]# Only target prod servers with "eu" tag, filter DB names[/]
  [cyan]db-runner[/] --tags prod,eu --dbfilter "^shop_" --sql patch.sql

  [dim]# CI/cron: fully non-interactive, save log as CSV[/]
  [cyan]db-runner[/] --sql fix.sql --no-vim --quiet --log-format csv --output run.csv

  [dim]# Retry failures 3 times, stop on first unrecoverable error[/]
  [cyan]db-runner[/] --sql update.sql --retry 3 --stop-on-error

  [dim]# Use stored procedures with custom delimiter[/]
  [cyan]db-runner[/] --sql procs.sql --delimiter "$$" --no-transaction

  [dim]# Load passwords from vault (keep connections.json password-free)[/]
  [cyan]db-runner[/] --vault ~/.db_vault --sql update.sql

[bold]WORKFLOW[/]
  [dim]1.[/] [cyan]connections.json[/] is read (searches [dim]./[/] then [dim]~/.config/db-runner/[/], or use [cyan]-c FILE[/])
  [dim]2.[/] Editor opens → write SQL [dim](:wq)[/]  [dim]Recent queries appear as comment lines[/]
  [dim]3.[/] DB lists are fetched from servers [dim](SHOW DATABASES)[/]
  [dim]4.[/] Editor opens → [cyan]server.db[/] list, delete lines you don't want
  [dim]5.[/] SQL is sent in parallel (per-server Semaphore)
  [dim]6.[/] Progress bar + ETA shown, waits for a keypress when done
  [dim]7.[/] Editor log buffer opens → save with [dim]:w file.log[/]

[bold]EDITOR[/]
  The editor is selected in order:
    [dim]1. $VISUAL[/]  environment variable
    [dim]2. $EDITOR[/]  environment variable
    [dim]3. vim[/]      (fallback)

[bold]CONFIGURATION[/]
  [cyan]connections.json[/] is searched in order:
    [dim]1. ./connections.json[/]           (current working directory)
    [dim]2. ~/.config/db-runner/connections.json[/]
  Use [cyan]-c FILE[/] to specify an explicit path.

  File format:
  [dim]{
    "name": "prod-1",        ← display name (optional, default: host)
    "host": "db.example.com",
    "port": 3306,            ← optional, default: 3306
    "user": "myuser",
    "password": "mypass",
    "max_connections": 3,    ← optional, default: 3
    "tags": ["prod", "eu"]  ← optional, used with --tags
  }[/]

[bold]VAULT FILE[/]
  A plain-text [dim]key=value[/] file, one entry per line:
  [dim]prod-1=secretpassword
  prod-2=anotherpassword[/]

[bold]HISTORY[/]
  Every executed SQL is saved to [cyan]~/.db_runner_history[/] (last 100 queries).
"""


def print_help() -> None:
    """Display the help page with rich formatting."""
    from rich.padding import Padding
    console.print(Padding(HELP_TEXT.strip(), (1, 2)))


# ---------------------------------------------------------------------------
# Wizard mode
# ---------------------------------------------------------------------------

def run_wizard() -> list[str]:
    """
    Interactive wizard that guides the user through every option.
    Returns a list of argv tokens to inject before re-parsing.
    """
    from rich.prompt import Confirm, Prompt, IntPrompt
    from rich.padding import Padding

    console.print()
    console.print(Panel(
        Align.center(
            "[bold cyan]db-runner  —  Setup Wizard[/]\n"
            "[dim]Answer each prompt to configure this run.\n"
            "Press Enter to accept the default value.[/]"
        ),
        border_style="cyan",
        padding=(1, 4),
    ))

    argv: list[str] = []

    def section(title: str) -> None:
        console.print(Rule(f"[bold cyan] {title} [/]", style="dim cyan"))

    # ── Connections ────────────────────────────────────────────────────────
    section("Connections")
    conn_file = Prompt.ask(
        "[bold]Connections file[/]",
        default="(auto)",
        console=console,
    )
    if conn_file and conn_file != "(auto)":
        argv += ["-c", conn_file]

    vault = Prompt.ask("[bold]Vault file[/] (leave blank to skip)", default="", console=console)
    if vault:
        argv += ["--vault", vault]

    server_filter = Prompt.ask("[bold]Filter servers[/] by name regex (blank = all)", default="", console=console)
    if server_filter:
        argv += ["--server", server_filter]

    tags = Prompt.ask("[bold]Filter by tags[/] (comma-separated, blank = all)", default="", console=console)
    if tags:
        argv += ["--tags", tags]

    # ── SQL ────────────────────────────────────────────────────────────────
    section("SQL")
    sql_source = Prompt.ask(
        "[bold]SQL source[/]",
        choices=["editor", "file", "stdin"],
        default="editor",
        console=console,
    )
    if sql_source == "file":
        sql_file = Prompt.ask("[bold]SQL file path[/]", console=console)
        argv += ["--sql", sql_file]
    elif sql_source == "stdin":
        argv += ["--no-vim"]

    delimiter = Prompt.ask("[bold]Statement delimiter[/]", default=";", console=console)
    if delimiter != ";":
        argv += ["--delimiter", delimiter]

    # ── Database filter ────────────────────────────────────────────────────
    section("Database filter")
    dbfilter = Prompt.ask("[bold]Include DBs matching regex[/] (blank = all)", default="", console=console)
    if dbfilter:
        argv += ["--dbfilter", dbfilter]

    exclude_db = Prompt.ask("[bold]Exclude DBs matching regex[/] (blank = none)", default="", console=console)
    if exclude_db:
        argv += ["--exclude-db", exclude_db]

    # ── Execution ──────────────────────────────────────────────────────────
    section("Execution")
    dry_run = Confirm.ask("[bold]Dry run?[/] (preview only, no execution)", default=False, console=console)
    if dry_run:
        argv += ["--dry-run"]

    use_transaction = Confirm.ask("[bold]Wrap each DB in a transaction?[/]", default=True, console=console)
    if not use_transaction:
        argv += ["--no-transaction"]

    force = Confirm.ask("[bold]Skip destructive-SQL confirmation?[/]", default=False, console=console)
    if force:
        argv += ["--force"]

    timeout = IntPrompt.ask("[bold]Query timeout[/] (seconds)", default=30, console=console)
    if timeout != 30:
        argv += ["--timeout", str(timeout)]

    concurrency = Prompt.ask("[bold]Global concurrency limit[/] (blank = per-server default)", default="", console=console)
    if concurrency.isdigit():
        argv += ["--concurrency", concurrency]

    retry = IntPrompt.ask("[bold]Retry failed DBs[/] N times (0 = no retry)", default=0, console=console)
    if retry:
        argv += ["--retry", str(retry)]

    delay = IntPrompt.ask("[bold]Per-DB delay[/] in ms (0 = none)", default=0, console=console)
    if delay:
        argv += ["--delay", str(delay)]

    stop_on_error = Confirm.ask("[bold]Stop on first error?[/]", default=False, console=console)
    if stop_on_error:
        argv += ["--stop-on-error"]

    # ── Output ─────────────────────────────────────────────────────────────
    section("Output")
    show_results = Confirm.ask("[bold]Show SELECT result rows in log?[/]", default=False, console=console)
    if show_results:
        argv += ["--show-results"]

    log_format = Prompt.ask(
        "[bold]Log format[/]",
        choices=["plain", "json", "csv"],
        default="plain",
        console=console,
    )
    if log_format != "plain":
        argv += ["--log-format", log_format]

    output_file = Prompt.ask("[bold]Save log to file[/] (blank = skip)", default="", console=console)
    if output_file:
        argv += ["--output", output_file]

    failed_output = Prompt.ask("[bold]Save failed DBs to file[/] (blank = skip)", default="", console=console)
    if failed_output:
        argv += ["--failed-output", failed_output]

    quiet = Confirm.ask("[bold]Quiet mode?[/] (no progress bar, no keypress)", default=False, console=console)
    if quiet:
        argv += ["--quiet"]

    no_partial_log = Confirm.ask("[bold]Suppress partial log on Ctrl+C?[/]", default=False, console=console)
    if no_partial_log:
        argv += ["--no-partial-log"]

    # ── Summary ────────────────────────────────────────────────────────────
    section("Summary")
    if argv:
        cmd = "db-runner " + " ".join(
            f'"{a}"' if " " in a else a for a in argv
        )
        console.print(f"[dim]Equivalent command:[/]\n  [cyan]{cmd}[/]\n")
    else:
        console.print("[dim]Running with all defaults.[/]\n")

    if not Confirm.ask("[bold]Proceed with these settings?[/]", default=True, console=console):
        console.print("[yellow]Cancelled.[/]")
        sys.exit(0)

    console.print()
    return argv


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="db-runner",
        description="db-runner: Bulk SQL execution tool for MySQL/MariaDB",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    parser.add_argument(
        "-c", "--connections",
        default=None,
        metavar="FILE",
        help="Connection config file (searches ./connections.json then ~/.config/db-runner/connections.json)",
    )
    parser.add_argument(
        "--sql",
        nargs="+",
        metavar="FILE",
        help="Read SQL from file(s) (opens editor if not specified; multiple files executed sequentially)",
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
        "--exclude-db",
        metavar="REGEX",
        help="Regex to exclude matching database names from the list",
    )
    parser.add_argument(
        "--server",
        metavar="REGEX",
        help="Filter connections by name/alias using a regex",
    )
    parser.add_argument(
        "--tags",
        metavar="TAG1,TAG2",
        help="Filter connections by tags (comma-separated; matches any)",
    )
    parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Stop execution on first failure",
    )
    parser.add_argument(
        "--retry",
        type=int,
        default=0,
        metavar="N",
        help="Retry failed databases N times with exponential backoff",
    )
    parser.add_argument(
        "--delay",
        type=int,
        default=0,
        metavar="MS",
        help="Per-database execution delay in milliseconds (rate limiting)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        metavar="N",
        help="Override per-server max_connections with a global concurrency limit",
    )
    parser.add_argument(
        "--delimiter",
        default=";",
        metavar="STR",
        help="Statement delimiter for splitting SQL (default: ';')",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress bar, keypress wait, and editor log (for CI/cron use)",
    )
    parser.add_argument(
        "--output",
        metavar="FILE",
        help="Save log to this file (format controlled by --log-format)",
    )
    parser.add_argument(
        "--no-vim",
        action="store_true",
        help="Skip all editor steps (SQL from stdin if --sql not given, select all DBs, no log viewer)",
    )
    parser.add_argument(
        "--vault",
        metavar="FILE",
        help="Key=value file to override connection passwords (format: connection_name=password)",
    )
    parser.add_argument(
        "--no-partial-log",
        action="store_true",
        help="Do not show the partial log when execution is interrupted with Ctrl+C",
    )
    parser.add_argument(
        "--wizard",
        action="store_true",
        help="Launch interactive setup wizard to configure all options before running",
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

    # Wizard: collect extra argv tokens, then re-parse with them merged
    if args.wizard:
        wizard_argv = run_wizard()
        # Re-parse: wizard flags take precedence over command-line defaults
        # but explicit non-wizard CLI flags (other than --wizard) win.
        original_argv = [a for a in sys.argv[1:] if a != "--wizard"]
        args = parser.parse_args(wizard_argv + original_argv)

    dry_run: bool = args.dry_run

    dry_badge = "\n\n[bold yellow]◆  DRY RUN  ─  no changes will be made[/]" if dry_run else ""
    console.print(Panel(
        Align.center(
            f"[bold cyan]db-runner[/]\n[dim]MySQL / MariaDB  ·  Bulk SQL Tool[/]{dry_badge}"
        ),
        border_style="cyan",
        padding=(1, 4),
        subtitle="[dim]Ctrl+C to cancel[/]",
    ))

    # 1. Load connections
    step_rule("Connections")
    resolved_path = find_connections_file(args.connections)
    connections = load_connections(args.connections, vault_path=args.vault)
    console.print(f"[green]✓[/] {len(connections)} server connection(s) loaded. [dim]({resolved_path})[/]")

    if args.server:
        connections = [c for c in connections if re.search(args.server, c["name"], re.IGNORECASE)]
        if not connections:
            console.print(f"[red]Error:[/] --server filter '{args.server}' matched no connections.")
            sys.exit(1)
        console.print(f"[dim]--server filter: {len(connections)} connection(s) matched.[/]")

    if args.tags:
        tag_list = [t.strip() for t in args.tags.split(",")]
        connections = [c for c in connections if any(t in c.get("tags", []) for t in tag_list)]
        if not connections:
            console.print(f"[red]Error:[/] --tags filter '{args.tags}' matched no connections.")
            sys.exit(1)
        console.print(f"[dim]--tags filter '{args.tags}': {len(connections)} connection(s) matched.[/]")

    # 2. Get SQL
    step_rule("SQL")
    if args.sql:
        sql_files = args.sql  # list of filenames
        sqls: list[tuple[Optional[str], str]] = []
        for fname in sql_files:
            try:
                with open(fname) as f:
                    content = f.read().strip()
            except FileNotFoundError:
                console.print(f"[red]Error:[/] SQL file not found: {fname}")
                sys.exit(1)
            if not content:
                console.print(f"[red]Error:[/] SQL file is empty: {fname}")
                sys.exit(1)
            sqls.append((fname, content))
            console.print(f"[green]✓[/] SQL read from file: {fname}")
    else:
        sql_text = get_sql_from_vim(no_vim=args.no_vim)
        console.print(f"[green]✓[/] SQL received ({len(sql_text.splitlines())} line(s)).")
        sqls = [(None, sql_text)]

    # 2b. Destructive check and history for all SQLs
    for _fname, sql_text in sqls:
        history_save(sql_text)
        check_destructive(sql_text, force=args.force)

    # 3. Fetch database lists
    step_rule("Databases")
    try:
        db_map = asyncio.run(fetch_all_databases(connections))
    except KeyboardInterrupt:
        console.print("\n[yellow]Cancelled.[/]")
        sys.exit(0)

    if not db_map:
        console.print("[red]Could not connect to any server, exiting.[/]")
        sys.exit(1)

    # 4. Filter database list
    selected = select_databases(db_map, dbfilter=args.dbfilter, exclude_db=args.exclude_db, no_vim=args.no_vim)

    if not selected:
        console.print("[yellow]No databases selected, exiting.[/]")
        sys.exit(0)

    console.print(f"[green]✓[/] {len(selected)} database(s) selected.\n")

    multi_file = len(sqls) > 1

    # 5-6. Execute SQL in parallel + progress (once per SQL file)
    step_rule("Execute")
    for fname, sql_text in sqls:
        if multi_file:
            console.print(Rule(f"[dim]{fname}[/]", style="dim", align="left"))
        partial_results: list[dict] = []
        interrupted = False
        try:
            asyncio.run(run_sql_on_all(
                selected, connections, sql_text,
                dry_run=dry_run,
                timeout=args.timeout,
                use_transaction=not args.no_transaction,
                show_results=args.show_results,
                stop_on_error=args.stop_on_error,
                retry=args.retry,
                delay_ms=args.delay,
                concurrency=args.concurrency,
                delimiter=args.delimiter,
                quiet=args.quiet,
                _results=partial_results,
            ))
        except KeyboardInterrupt:
            interrupted = True
            console.print("\n[yellow]⚠ Execution interrupted by user.[/]")

        results = partial_results

        # 7. Show log — always show partial results unless --no-partial-log
        if interrupted and (args.no_partial_log or not results):
            if not results:
                console.print("[dim]No operations completed before interrupt.[/]")
            sys.exit(0)

        show_log(
            results, sql_text,
            dry_run=dry_run,
            log_format=args.log_format,
            failed_output=args.failed_output,
            sql_file_label=fname if multi_file else None,
            quiet=args.quiet,
            output_file=args.output,
            no_vim=args.no_vim,
            interrupted=interrupted,
        )

        if interrupted:
            sys.exit(0)


if __name__ == "__main__":
    main()
