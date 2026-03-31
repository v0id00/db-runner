#!/usr/bin/env python3
"""
db-runner: MySQL/MariaDB için toplu SQL gönderme aracı

Kullanım:
  python db_runner.py                        # connections.json kullan, vim ile SQL al
  python db_runner.py -c /path/to/conn.json  # farklı config dosyası
  python db_runner.py --sql query.sql         # SQL'i dosyadan oku
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
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
# 2. Vim entegrasyonu
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
    """Kullanıcıdan vim aracılığıyla SQL girdisi al."""
    console.print("\n[bold cyan]► Vim açılıyor[/] — SQL'i yazın, kaydedin ve çıkın [dim](:wq)[/]\n")

    template = (
        "-- SQL'i buraya yazın\n"
        "-- Birden fazla ifade için noktalı virgül (;) kullanın\n"
        "-- Hazır olunca: :wq\n\n"
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

async def execute_on_db(
    conn: dict,
    db_name: str,
    sql: str,
    semaphore: asyncio.Semaphore,
    progress: Progress,
    task_id: object,
    results: list,
) -> None:
    """Bir veritabanında SQL çalıştır (semaphore ile hız sınırlaması)."""
    server_name = conn["name"]
    async with semaphore:
        try:
            connection = await aiomysql.connect(
                host=conn["host"],
                port=conn["port"],
                user=conn["user"],
                password=conn["password"],
                db=db_name,
                connect_timeout=30,
                autocommit=True,
            )
            try:
                async with connection.cursor() as cursor:
                    await cursor.execute(sql)
                    affected = cursor.rowcount
                    results.append({
                        "server": server_name,
                        "db": db_name,
                        "status": "OK",
                        "affected": affected,
                        "error": None,
                    })
            finally:
                connection.close()
        except Exception as e:
            results.append({
                "server": server_name,
                "db": db_name,
                "status": "ERR",
                "affected": 0,
                "error": str(e),
            })
        finally:
            progress.advance(task_id)


async def run_sql_on_all(
    selected: list[tuple[str, str]],
    connections: list[dict],
    sql: str,
) -> list[dict]:
    """Seçili veritabanlarına paralel SQL gönder."""
    conn_map = {conn["name"]: conn for conn in connections}

    semaphores: dict[str, asyncio.Semaphore] = {
        server: asyncio.Semaphore(conn_map[server].get("max_connections", 3))
        for server in {s for s, _ in selected}
        if server in conn_map
    }

    results: list[dict] = []

    with Progress(
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
    ) as progress:
        task_id = progress.add_task("[cyan]SQL gönderiliyor...", total=len(selected))

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
                )
            )

        await asyncio.gather(*tasks)

    return results


# ---------------------------------------------------------------------------
# 7. Log görüntüleme
# ---------------------------------------------------------------------------

def show_log(results: list[dict], sql: str) -> None:
    """Sonuçları özet panelde ve vim'de göster."""
    ok_count = sum(1 for r in results if r["status"] == "OK")
    err_count = len(results) - ok_count
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Terminale özet
    summary_color = "green" if err_count == 0 else "yellow" if ok_count > 0 else "red"
    console.print()
    console.print(Panel(
        f"[green]Başarılı:[/] {ok_count}   [red]Hatalı:[/] {err_count}   "
        f"[dim]Toplam: {len(results)}[/]",
        title="[bold]Sonuç Özeti[/]",
        border_style=summary_color,
    ))

    # Log içeriği oluştur
    header_lines = [
        f"# db-runner Log — {timestamp}",
        f"# Toplam: {len(results)}  Başarılı: {ok_count}  Hatalı: {err_count}",
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

    result_lines = []
    for r in sorted(results, key=lambda x: (x["status"] != "ERR", x["server"], x["db"])):
        if r["status"] == "OK":
            result_lines.append(f"[OK]  {r['server']}:{r['db']}  affected={r['affected']}")
        else:
            result_lines.append(f"[ERR] {r['server']}:{r['db']}  {r['error']}")

    log_content = "\n".join(header_lines + result_lines) + "\n"

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
        ),
    )
    parser.add_argument(
        "-c", "--connections",
        default="connections.json",
        metavar="DOSYA",
        help="Bağlantı konfigürasyon dosyası (varsayılan: connections.json)",
    )
    parser.add_argument(
        "--sql",
        metavar="DOSYA",
        help="SQL'i dosyadan oku (belirtilmezse vim açılır)",
    )
    args = parser.parse_args()

    console.print(Panel(
        "[bold cyan]db-runner[/]  —  MySQL/MariaDB Toplu SQL Aracı\n"
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
        results = asyncio.run(run_sql_on_all(selected, connections, sql))
    except KeyboardInterrupt:
        console.print("\n[yellow]İşlem kesildi.[/]")
        sys.exit(0)

    # 7. Log göster
    show_log(results, sql)


if __name__ == "__main__":
    main()
