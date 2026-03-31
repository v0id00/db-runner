#!/usr/bin/env python3
"""
db-runner: MySQL/MariaDB için toplu SQL gönderme aracı

Kullanım:
  python db_runner.py                        # connections.json kullan, vim ile SQL al
  python db_runner.py -c /path/to/conn.json  # farklı config dosyası
  python db_runner.py --sql query.sql         # SQL'i dosyadan oku
"""

import argparse
import csv
import io
import asyncio
import json
import os
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
    print("Hata: aiomysql modülü gerekli. Kurmak için: pip install aiomysql", file=sys.stderr)
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
    SQL tehlikeli keyword içeriyorsa uyarı göster, onay iste.
    `force=True` ise onay atlanır.
    """
    import re
    found = [
        kw.replace(r"\b", "").replace(r"\s+", " ")
        for kw in DESTRUCTIVE_KEYWORDS
        if re.search(kw, sql, re.IGNORECASE)
    ]
    if not found:
        return

    console.print()
    console.print(Panel(
        "[bold red]⚠ Tehlikeli SQL tespit edildi![/]\n\n"
        f"Bulunan anahtar kelimeler: [red]{', '.join(found)}[/]\n\n"
        "[dim]Devam etmek için onaylayın, iptal için Ctrl+C[/]",
        border_style="red",
        title="[bold red]Uyarı[/]",
    ))

    if force:
        console.print("[yellow]--force ile onay atlandı.[/]")
        return

    console.print("[bold]Devam etmek için [red]EVET[/] yazın:[/] ", end="")
    try:
        answer = input()
    except (EOFError, KeyboardInterrupt):
        console.print("\n[yellow]İptal edildi.[/]")
        sys.exit(0)

    if answer.strip().upper() != "EVET":
        console.print("[yellow]İptal edildi.[/]")
        sys.exit(0)


# ---------------------------------------------------------------------------
# 1. Bağlantı konfigürasyonu
# ---------------------------------------------------------------------------

def load_connections(path: str) -> list[dict]:
    """connections.json dosyasını yükle ve doğrula."""
    try:
        with open(path) as f:
            conns = json.load(f)
    except FileNotFoundError:
        console.print(f"[red]Hata:[/] '{path}' bulunamadı.")
        sys.exit(1)
    except json.JSONDecodeError as e:
        console.print(f"[red]JSON parse hatası:[/] {e}")
        sys.exit(1)

    if not isinstance(conns, list) or len(conns) == 0:
        console.print("[red]Hata:[/] connections.json boş veya liste formatında değil.")
        sys.exit(1)

    required = {"host", "user", "password"}
    for i, conn in enumerate(conns):
        missing = required - conn.keys()
        if missing:
            console.print(f"[red]Hata:[/] Bağlantı #{i} eksik alanlar: {', '.join(missing)}")
            sys.exit(1)
        conn.setdefault("port", 3306)
        conn.setdefault("name", conn["host"])
        conn.setdefault("max_connections", 3)

    return conns


# ---------------------------------------------------------------------------
# 2. SQL geçmişi
# ---------------------------------------------------------------------------

HISTORY_FILE = os.path.expanduser("~/.db_runner_history")
HISTORY_MAX  = 100   # saklanacak maksimum giriş sayısı
HISTORY_SHOW = 10    # vim'de önizlemede gösterilecek son N giriş


def history_load() -> list[dict]:
    """~/.db_runner_history dosyasını JSON satırları olarak oku."""
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
    """SQL'i geçmiş dosyasına ekle, HISTORY_MAX aşılırsa eskiyi sil."""
    entry = {"ts": datetime.now().isoformat(timespec="seconds"), "sql": sql}
    entries = history_load()
    entries.append(entry)
    entries = entries[-HISTORY_MAX:]
    try:
        with open(HISTORY_FILE, "w") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
    except OSError as exc:
        console.print(f"[yellow]Uyarı: geçmiş kaydedilemedi:[/] {exc}")


def history_comment_block() -> str:
    """Son HISTORY_SHOW girişi vim şablonunun üstüne yorum olarak ekle."""
    entries = history_load()
    if not entries:
        return ""
    recent = entries[-HISTORY_SHOW:][::-1]  # en yeni üstte
    lines = ["-- ──── Son sorgular (geçmiş) ────────────────────────────────"]
    for e in recent:
        ts = e.get("ts", "")
        for i, sql_line in enumerate(e["sql"].splitlines()):
            prefix = f"-- [{ts}] " if i == 0 else "--          "
            lines.append(f"{prefix}{sql_line}")
    lines.append("-- ─────────────────────────────────────────────────────────")
    lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 3. Vim entegrasyonu
# ---------------------------------------------------------------------------

def open_vim(content: str, suffix: str = ".txt", comment: str = "") -> str:
    """
    Geçici dosyayı vim ile aç. Kaydedilen içeriği döndür.
    Vim'den çıkış kodu sıfır değilse (ya da içerik değişmediyse) boş string döner.
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
    """Kullanıcıdan vim aracılığıyla SQL girdisi al (geçmişi şablona ekle)."""
    console.print("\n[bold cyan]► Vim açılıyor[/] — SQL'i yazın, kaydedin ve çıkın [dim](:wq)[/]\n")

    history_block = history_comment_block()
    template = (
        history_block
        + "-- SQL'i buraya yazın\n"
        + "-- Birden fazla ifade için noktalı virgül (;) kullanın\n"
        + "-- Hazır olunca: :wq\n\n"
    )
    content = open_vim(template, suffix=".sql")

    sql_lines = [
        line for line in content.splitlines()
        if line.strip() and not line.strip().startswith("--")
    ]
    sql = "\n".join(sql_lines).strip()

    if not sql:
        console.print("[yellow]SQL boş, çıkılıyor.[/]")
        sys.exit(0)

    return sql


# ---------------------------------------------------------------------------
# 3. Veritabanı listelerini çekme
# ---------------------------------------------------------------------------

async def fetch_databases_for(conn: dict) -> tuple[str, list[str], Optional[str]]:
    """Tek bir sunucudan veritabanı listesi çek."""
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
    """Tüm sunuculardan eşzamanlı olarak veritabanı listesi çek."""
    console.print("\n[bold]Veritabanı listeleri çekiliyor...[/]")

    tasks = [fetch_databases_for(conn) for conn in connections]
    results = await asyncio.gather(*tasks)

    db_map: dict[str, list[str]] = {}
    for name, dbs, error in results:
        if error:
            console.print(f"  [red]✗ {name}:[/] {error}")
        else:
            console.print(f"  [green]✓ {name}:[/] {len(dbs)} veritabanı")
            if dbs:
                db_map[name] = dbs

    return db_map


# ---------------------------------------------------------------------------
# 4. Veritabanı seçimi (vim ile filtreleme)
# ---------------------------------------------------------------------------

def select_databases(db_map: dict[str, list[str]]) -> list[tuple[str, str]]:
    """
    Tüm DB'leri vim'de göster, kullanıcı göndermek istemediklerini siler.
    (sunucu_adi, db_adi) çiftlerinin listesini döndür.
    """
    all_entries = []
    for server_name, dbs in db_map.items():
        for db in sorted(dbs):
            all_entries.append(f"{server_name}:{db}")

    if not all_entries:
        console.print("[red]Hiç veritabanı bulunamadı, çıkılıyor.[/]")
        sys.exit(0)

    comment = (
        "# ──────────────────────────────────────────────────────────────\n"
        "# Göndermek İSTEMEDİĞİNİZ satırları silin veya # ile başlatın\n"
        "# Satır formatı: sunucu_adi:veritabani_adi\n"
        "# Tümünü seçmek için doğrudan kaydedin: :wq\n"
        "# ──────────────────────────────────────────────────────────────\n\n"
    )

    total = len(all_entries)
    console.print(
        f"\n[bold cyan]► Vim açılıyor[/] — [bold]{total}[/] veritabanı listelendi. "
        "Göndermek istemediklerinizi silin.\n"
    )

    content = open_vim("\n".join(all_entries) + "\n", suffix=".txt", comment=comment)

    selected: list[tuple[str, str]] = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        server_name, db_name = line.split(":", 1)
        server_name, db_name = server_name.strip(), db_name.strip()
        if server_name and db_name:
            selected.append((server_name, db_name))

    return selected


# ---------------------------------------------------------------------------
# 5-6. Paralel SQL çalıştırma + progress bar
# ---------------------------------------------------------------------------

def wait_for_keypress(prompt: str = "\n[dim]Devam etmek için bir tuşa basın...[/]") -> None:
    """Terminalde tek tuş bekle (echo yok)."""
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
    """Bir veritabanında SQL çalıştır (semaphore ile hız sınırlaması)."""
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
    """Seçili veritabanlarına paralel SQL gönder."""
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
    task_id = progress.add_task(f"[cyan]SQL gönderiliyor...{dry_label}", total=len(selected))

    tasks = []
    for server_name, db_name in selected:
        if server_name not in conn_map:
            results.append({
                "server": server_name,
                "db": db_name,
                "status": "ERR",
                "affected": 0,
                "error": f"Sunucu tanımsız: {server_name}",
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
        done_label = f"[yellow]DRY RUN — {dry_count} veritabanı hedeflendi[/]"
    else:
        status_color = "green" if err == 0 else "yellow" if ok > 0 else "red"
        done_label = f"[{status_color}]✓ Tamamlandı[/]  [green]{ok} başarılı[/]  [red]{err} hatalı[/]"
    progress.update(task_id, description=done_label)
    progress.stop()

    wait_for_keypress()

    return results


# ---------------------------------------------------------------------------
# 7. Log görüntüleme
# ---------------------------------------------------------------------------

def format_results(
    results: list[dict],
    sql: str,
    log_format: str,
    dry_run: bool,
    timestamp: str,
) -> str:
    """Sonuçları istenen formatta string'e dönüştür."""
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

    # plain (varsayılan)
    dry_tag = "  [DRY RUN]" if dry_run else ""
    lines = [
        f"# db-runner Log — {timestamp}{dry_tag}",
        f"# Toplam: {len(results)}  Başarılı: {ok_count}  Hatalı: {err_count}"
        + (f"  DryRun: {dry_count}" if dry_run else ""),
        "#",
        "# Çalıştırılan SQL:",
        *[f"#   {line}" for line in sql.splitlines()],
        "#",
        "# ─────────────────────────────────────────────────────────────",
        "# Kaydetmek için:  :w /tam/yol/dosya.log",
        "# Çıkmak için:     :q",
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
    """Sonuçları özet panelde ve vim'de göster; isteğe bağlı dosyaya kaydet."""
    dry_count = sum(1 for r in results if r["status"] == "DRY")
    ok_count  = sum(1 for r in results if r["status"] == "OK")
    err_count = sum(1 for r in results if r["status"] == "ERR")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Terminale özet
    if dry_run:
        summary_color = "yellow"
        summary_text = f"[yellow]DRY RUN[/]  {dry_count} veritabanı hedeflenirdi"
    else:
        summary_color = "green" if err_count == 0 else "yellow" if ok_count > 0 else "red"
        summary_text = (
            f"[green]Başarılı:[/] {ok_count}   [red]Hatalı:[/] {err_count}   "
            f"[dim]Toplam: {len(results)}[/]"
        )
    console.print()
    console.print(Panel(
        summary_text,
        title="[bold]Sonuç Özeti[/]",
        border_style=summary_color,
    ))

    # Hatalı DB'leri ayrı dosyaya kaydet
    failed_results = [r for r in results if r["status"] == "ERR"]
    if failed_output and failed_results:
        try:
            with open(failed_output, "w") as f:
                for r in failed_results:
                    f.write(f"{r['server']}:{r['db']}\n")
            console.print(f"[yellow]⚠[/] {len(failed_results)} hatalı DB '{failed_output}' dosyasına kaydedildi.")
        except OSError as exc:
            console.print(f"[red]Hata: failed-output yazılamadı:[/] {exc}")

    log_content = format_results(results, sql, "plain", dry_run, timestamp)

    console.print("\n[bold cyan]► Vim açılıyor[/] — log görüntüleniyor [dim](:w dosya.log ile kaydet, :q ile çık)[/]\n")

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
# Giriş noktası
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="db-runner: MySQL/MariaDB için toplu SQL gönderme aracı",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Örnekler:\n"
            "  python db_runner.py\n"
            "  python db_runner.py -c /etc/db_runner/connections.json\n"
            "  python db_runner.py --sql update.sql\n"
            "  python db_runner.py --dry-run\n"
        ),
    )
    parser.add_argument(
        "-c", "--connections",
        default="connections.json",
        metavar="DOSYA",
        help="Bağlantı konfigürasyon dosyası (varsayılan: connections.json — örnek: connections.example.json)",
    )
    parser.add_argument(
        "--sql",
        metavar="DOSYA",
        help="SQL'i dosyadan oku (belirtilmezse vim açılır)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="SQL çalıştırmadan hedef veritabanlarını göster",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Destructive SQL (DROP/TRUNCATE/DELETE/ALTER TABLE) onayını atla",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        metavar="SANİYE",
        help="Sorgu timeout süresi saniye cinsinden (varsayılan: 30)",
    )
    parser.add_argument(
        "--no-transaction",
        action="store_true",
        help="Transaction kullanma, autocommit modunda çalış",
    )
    parser.add_argument(
        "--log-format",
        choices=["plain", "json", "csv"],
        default="plain",
        help="Log kayıt formatı: plain (varsayılan), json, csv",
    )
    parser.add_argument(
        "--failed-output",
        metavar="DOSYA",
        help="Hatalı DB'leri sunucu:db formatında bu dosyaya kaydet",
    )
    parser.add_argument(
        "--show-results",
        action="store_true",
        help="SELECT sorgularının döndürdüğü satırları logda göster",
    )
    args = parser.parse_args()

    dry_run: bool = args.dry_run

    header_extra = "  [yellow bold][DRY RUN][/]" if dry_run else ""
    console.print(Panel(
        f"[bold cyan]db-runner[/]  —  MySQL/MariaDB Toplu SQL Aracı{header_extra}\n"
        "[dim]Çıkmak için istediğiniz zaman Ctrl+C kullanabilirsiniz[/]",
        border_style="cyan",
    ))

    # 1. Bağlantıları yükle
    connections = load_connections(args.connections)
    console.print(f"[green]✓[/] {len(connections)} sunucu bağlantısı yüklendi.")

    # 2. SQL al
    if args.sql:
        try:
            with open(args.sql) as f:
                sql = f.read().strip()
        except FileNotFoundError:
            console.print(f"[red]Hata:[/] SQL dosyası bulunamadı: {args.sql}")
            sys.exit(1)
        if not sql:
            console.print("[red]Hata:[/] SQL dosyası boş.")
            sys.exit(1)
        console.print(f"[green]✓[/] SQL dosyadan okundu: {args.sql}")
    else:
        sql = get_sql_from_vim()
        console.print(f"[green]✓[/] SQL alındı ({len(sql.splitlines())} satır).")

    history_save(sql)

    # 2b. Destructive keyword kontrolü
    check_destructive(sql, force=args.force)

    # 3. Veritabanı listelerini çek
    try:
        db_map = asyncio.run(fetch_all_databases(connections))
    except KeyboardInterrupt:
        console.print("\n[yellow]İptal edildi.[/]")
        sys.exit(0)

    if not db_map:
        console.print("[red]Hiçbir sunucuya bağlanılamadı, çıkılıyor.[/]")
        sys.exit(1)

    # 4. Veritabanı listesini filtrele
    selected = select_databases(db_map)

    if not selected:
        console.print("[yellow]Hiç veritabanı seçilmedi, çıkılıyor.[/]")
        sys.exit(0)

    console.print(f"[green]✓[/] {len(selected)} veritabanı seçildi.\n")

    # 5-6. Paralel SQL çalıştır + progress
    try:
        results = asyncio.run(run_sql_on_all(
            selected, connections, sql,
            dry_run=dry_run,
            timeout=args.timeout,
            use_transaction=not args.no_transaction,
            show_results=args.show_results,
        ))
    except KeyboardInterrupt:
        console.print("\n[yellow]İşlem kesildi.[/]")
        sys.exit(0)

    # 7. Log göster
    show_log(results, sql, dry_run=dry_run, log_format=args.log_format, failed_output=args.failed_output)


if __name__ == "__main__":
    main()
