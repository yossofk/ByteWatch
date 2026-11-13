"""
data_layer.py
=============
Hardware-verified Network Usage Tracker data layer with multi-day range queries.
"""

import os
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta

import psutil

DB_FILENAME = "network_usage.db"


def get_db_path(directory: str) -> str:
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, DB_FILENAME)


SCHEMA = """
CREATE TABLE IF NOT EXISTS process_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT    NOT NULL,
    taken_at        TEXT    NOT NULL,
    exe_path        TEXT    NOT NULL,
    process_name    TEXT    NOT NULL,
    pid             INTEGER NOT NULL,
    bytes_sent      REAL    NOT NULL,
    bytes_recv      REAL    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snap_date ON process_snapshots (date);
CREATE INDEX IF NOT EXISTS idx_snap_exe ON process_snapshots (exe_path);

CREATE TABLE IF NOT EXISTS daily_usage (
    date            TEXT    NOT NULL,
    process_name    TEXT    NOT NULL,
    exe_path        TEXT    NOT NULL,
    upload_bytes    REAL    NOT NULL,
    download_bytes  REAL    NOT NULL,
    sample_count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (date, exe_path)
);
"""


class UsageDatabase:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._local = threading.local()
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return self._local.conn

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    def record_deltas(self, sample_date: str, taken_at: str, records: list) -> None:
        if not records:
            return
        with self._conn() as conn:
            conn.executemany(
                "INSERT INTO process_snapshots "
                "(date, taken_at, exe_path, process_name, pid, bytes_sent, bytes_recv) "
                "VALUES (?,?,?,?,?,?,?)",
                [(sample_date, taken_at, r[0], r[1], r[2], r[3], r[4])
                 for r in records if isinstance(r, (list, tuple)) and len(r) >= 5],
            )
            for exe_path, process_name, pid, sent_delta, recv_delta in records:
                conn.execute("""
                    INSERT INTO daily_usage (date, process_name, exe_path,
                                             upload_bytes, download_bytes, sample_count)
                    VALUES (?, ?, ?, ?, ?, 1)
                    ON CONFLICT(date, exe_path) DO UPDATE SET
                        upload_bytes   = upload_bytes + excluded.upload_bytes,
                        download_bytes = download_bytes + excluded.download_bytes,
                        sample_count   = sample_count + 1,
                        process_name   = excluded.process_name
                """, (sample_date, process_name, exe_path, sent_delta, recv_delta))
            conn.commit()

    def daily_totals(self, limit_days: int = None) -> list:
        with self._conn() as conn:
            query = """
                SELECT date,
                       SUM(upload_bytes)   AS upload,
                       SUM(download_bytes) AS download,
                       COUNT(*)            AS app_count
                FROM daily_usage
                GROUP BY date
                ORDER BY date DESC
            """
            if limit_days and limit_days > 0:
                query += f" LIMIT {int(limit_days)}"
            rows = conn.execute(query).fetchall()
        return [(r["date"], r["upload"] or 0, r["download"] or 0, r["app_count"])
                for r in rows]

    def day_breakdown(self, day: date) -> list:
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT process_name, exe_path, upload_bytes,
                       download_bytes,
                       (upload_bytes + download_bytes) AS total_bytes,
                       sample_count
                FROM daily_usage
                WHERE date = ?
                ORDER BY total_bytes DESC
            """, (day.isoformat(),)).fetchall()
        return [(r["process_name"], r["exe_path"], r["upload_bytes"] or 0,
                 r["download_bytes"] or 0, r["total_bytes"] or 0, r["sample_count"])
                for r in rows]

    def range_breakdown(self, start_date: str, end_date: str) -> list:
        """Roll up consumption for each app between two dates (inclusive)."""
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT process_name, exe_path,
                       SUM(upload_bytes) AS upload_bytes,
                       SUM(download_bytes) AS download_bytes,
                       SUM(upload_bytes + download_bytes) AS total_bytes,
                       SUM(sample_count) AS sample_count
                FROM daily_usage
                WHERE date BETWEEN ? AND ?
                GROUP BY exe_path
                ORDER BY total_bytes DESC
            """, (start_date, end_date)).fetchall()
        return [(r["process_name"], r["exe_path"], r["upload_bytes"] or 0,
                 r["download_bytes"] or 0, r["total_bytes"] or 0, r["sample_count"])
                for r in rows]

    def purge_older_than(self, days: int) -> None:
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        with self._conn() as conn:
            conn.execute("DELETE FROM process_snapshots WHERE date < ?", (cutoff,))
            conn.execute("DELETE FROM daily_usage WHERE date < ?", (cutoff,))
            conn.commit()

    def vacuum(self) -> None:
        with self._conn() as conn:
            conn.execute("VACUUM")

    def hourly_today(self) -> list:
        """Return hourly upload/download totals for today.
        Returns list of (hour: int, upload: float, download: float) for hours 0-23.
        """
        today = date.today().isoformat()
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT CAST(strftime('%H', taken_at) AS INTEGER) AS hour,
                       SUM(bytes_sent) AS upload,
                       SUM(bytes_recv) AS download
                FROM process_snapshots
                WHERE date = ?
                GROUP BY hour
                ORDER BY hour
            """, (today,)).fetchall()
        result = {r["hour"]: (r["upload"] or 0.0, r["download"] or 0.0) for r in rows}
        return [(h, result.get(h, (0.0, 0.0))[0], result.get(h, (0.0, 0.0))[1]) for h in range(24)]

    def monthly_stats(self) -> tuple:
        """Return (current_month_total, avg_monthly_total) in bytes."""
        today = date.today()
        month_prefix = today.strftime("%Y-%m")
        with self._conn() as conn:
            cur = conn.execute("""
                SELECT SUM(upload_bytes + download_bytes)
                FROM daily_usage WHERE date LIKE ?
            """, (month_prefix + "%",)).fetchone()
            current_month = float(cur[0] or 0)

            avg_row = conn.execute("""
                SELECT AVG(monthly_total) FROM (
                    SELECT SUM(upload_bytes + download_bytes) AS monthly_total
                    FROM daily_usage
                    WHERE date NOT LIKE ?
                    GROUP BY strftime('%Y-%m', date)
                )
            """, (month_prefix + "%",)).fetchone()
            avg_monthly = float(avg_row[0] or 0)
        return (current_month, avg_monthly)


# ---------------------------------------------------------------------------
# Hardware-Bounded Sampling
# ---------------------------------------------------------------------------

_last_process_counters = {}
_last_global_net = None


def sample_process_deltas():
    global _last_process_counters, _last_global_net

    current_global_net = psutil.net_io_counters()
    if _last_global_net is None:
        _last_global_net = current_global_net
        return []

    real_global_sent = max(0.0, float(current_global_net.bytes_sent - _last_global_net.bytes_sent))
    real_global_recv = max(0.0, float(current_global_net.bytes_recv - _last_global_net.bytes_recv))
    _last_global_net = current_global_net

    if real_global_sent == 0 and real_global_recv == 0:
        return []

    active_network_pids = set()
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.pid and c.raddr and c.status == "ESTABLISHED":
                ip = getattr(c.raddr, "ip", "")
                if ip and not ip.startswith("127.") and ip != "::1":
                    active_network_pids.add(c.pid)
    except Exception:
        pass

    raw_candidates = []
    current_counters = {}

    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            pid = proc.info["pid"]
            exe = proc.info.get("exe")
            name = (proc.info.get("name") or "").lower()

            if not exe or pid == 0:
                continue

            try:
                io = proc.io_counters()
                write_bytes = float(getattr(io, "write_bytes", 0) or 0)
                read_bytes = float(getattr(io, "read_bytes", 0) or 0)
            except Exception:
                continue

            current_counters[pid] = (write_bytes, read_bytes)

            if pid in _last_process_counters and pid in active_network_pids:
                prev_w, prev_r = _last_process_counters[pid]
                d_w = max(0.0, write_bytes - prev_w)
                d_r = max(0.0, read_bytes - prev_r)
                if d_w > 0 or d_r > 0:
                    raw_candidates.append((exe, name, pid, d_w, d_r))
        except Exception:
            continue

    _last_process_counters = current_counters

    if not raw_candidates:
        return []

    total_raw_sent = sum(r[3] for r in raw_candidates)
    total_raw_recv = sum(r[4] for r in raw_candidates)

    final_records = []
    for exe, name, pid, d_w, d_r in raw_candidates:
        alloc_sent = (d_w / total_raw_sent * real_global_sent) if total_raw_sent > 0 else 0.0
        alloc_recv = (d_r / total_raw_recv * real_global_recv) if total_raw_recv > 0 else 0.0

        if alloc_sent > 0 or alloc_recv > 0:
            final_records.append((exe, name, pid, alloc_sent, alloc_recv))

    return final_records


class UsageCollector:
    def __init__(self, db: UsageDatabase, interval_seconds: int = 15, retention_days: int = 180):
        self.db = db
        self.interval = interval_seconds
        self.retention_days = retention_days
        self._stop = threading.Event()
        self._thread = None
        self._last_day = date.today().isoformat()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        sample_process_deltas()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def collect(self) -> list:
        now = datetime.now()
        records = sample_process_deltas()
        self.db.record_deltas(now.date().isoformat(), now.isoformat(timespec="seconds"), records)
        return records

    def run_once(self) -> int:
        now = datetime.now()
        today_str = now.date().isoformat()
        records = sample_process_deltas()
        self.db.record_deltas(today_str, now.isoformat(timespec="seconds"), records)

        if today_str != self._last_day:
            self.db.purge_older_than(self.retention_days)
            self.db.vacuum()
            self._last_day = today_str
        return len(records)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                pass
            self._stop.wait(self.interval)