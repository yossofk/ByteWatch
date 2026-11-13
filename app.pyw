"""
app.pyw
=======
ByteWatch – Redesigned Dashboard.
Matches target UI: compact header with live speed, KPI cards,
Visual Analytics canvas chart, Daily Log History with CSV export.
All DB reads run on background threads to keep the main UI thread free.
"""

import csv
import math
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
import winreg
from datetime import date, datetime, timedelta
from tkinter import filedialog

import customtkinter as ctk
from PIL import Image, ImageDraw
import psutil
import pystray

from data_layer import UsageCollector, UsageDatabase, get_db_path

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)

REG_KEY  = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "ByteWatch"

# ── Colour palette ────────────────────────────────────────────────────────────
BG_MAIN   = "#0d1117"
BG_CARD   = "#161b22"
BG_ROW_A  = "#1e293b"
BG_ROW_B  = "#182234"
BG_HEADER = "#1c2333"
BG_CHART  = "#0f172a"
COL_UP    = "#6366f1"   # indigo – upload
COL_DOWN  = "#06b6d4"   # cyan   – download
COL_TEXT  = "#e2e8f0"
COL_DIM   = "#64748b"
COL_GREEN = "#22c55e"
COL_ACCENT= "#38bdf8"
COL_BTN   = "#1e293b"
COL_BTN_H = "#2d3f56"

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")


# ── Helpers ───────────────────────────────────────────────────────────────────

def fmt_bytes(value: float) -> str:
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        value = 0.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024.0:
            return f"{value:,.2f} {unit}"
        value /= 1024.0
    return f"{value:,.2f} PB"


def fmt_speed(bps: float) -> str:
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if bps < 1024.0:
            return f"{bps:.1f} {unit}"
        bps /= 1024.0
    return f"{bps:.1f} TB/s"


def format_app_name(name: str, limit: int = 24) -> str:
    if name.lower().endswith(".exe"):
        name = name[:-4]
    if len(name) > limit:
        return name[:limit - 3] + "..."
    return name


def open_folder(file_path: str) -> None:
    try:
        norm = os.path.normpath(file_path)
        if os.path.exists(norm):
            subprocess.Popen(f'explorer /select,"{norm}"')
        else:
            folder = os.path.dirname(norm)
            if os.path.exists(folder):
                os.startfile(folder)
    except Exception as e:
        print(f"Failed to open directory: {e}")


# ── Windows registry helpers ──────────────────────────────────────────────────

def set_startup(enable: bool) -> None:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY, 0, winreg.KEY_SET_VALUE)
        if enable:
            py_dir     = os.path.dirname(sys.executable)
            pythonw    = os.path.join(py_dir, "pythonw.exe")
            if not os.path.exists(pythonw):
                pythonw = sys.executable
            cmd = f'"{pythonw}" "{os.path.abspath(__file__)}" --tray'
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception as e:
        print(f"Startup registry error: {e}")


def is_startup_enabled() -> bool:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY, 0, winreg.KEY_READ)
        winreg.QueryValueEx(key, APP_NAME)
        winreg.CloseKey(key)
        return True
    except FileNotFoundError:
        return False


# ── Tray icon ─────────────────────────────────────────────────────────────────

def create_tray_image():
    img  = Image.new("RGBA", (64, 64), color=(0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((6, 6, 58, 58), fill="#1f538d", outline="#3b82f6", width=4)
    draw.ellipse((22, 22, 42, 42), fill="#60a5fa")
    return img


# ── Canvas chart helpers ──────────────────────────────────────────────────────

def _smooth_points(values: list, x0, y0, w, h, max_val: float):
    """Map (index, value) pairs to canvas (x, y) coords with Catmull-Rom style smoothing.
    Returns flat list of coords for create_line."""
    n = len(values)
    if n < 2:
        return []
    pts = []
    for i, v in enumerate(values):
        x = x0 + i * w / (n - 1)
        y = y0 + h - (v / max_val * h) if max_val > 0 else y0 + h
        pts.append((x, y))

    # Catmull-Rom spline – insert intermediate control points
    out = []
    tension = 0.4
    for i in range(len(pts)):
        p0 = pts[max(i - 1, 0)]
        p1 = pts[i]
        p2 = pts[min(i + 1, len(pts) - 1)]
        p3 = pts[min(i + 2, len(pts) - 1)]
        for t in [0.0, 0.33, 0.67] if i < len(pts) - 1 else [0.0]:
            t2, t3 = t * t, t * t * t
            x = (0.5 * ((2 * p1[0]) +
                        (-p0[0] + p2[0]) * t +
                        (2*p0[0] - 5*p1[0] + 4*p2[0] - p3[0]) * t2 +
                        (-p0[0] + 3*p1[0] - 3*p2[0] + p3[0]) * t3))
            y = (0.5 * ((2 * p1[1]) +
                        (-p0[1] + p2[1]) * t +
                        (2*p0[1] - 5*p1[1] + 4*p2[1] - p3[1]) * t2 +
                        (-p0[1] + 3*p1[1] - 3*p2[1] + p3[1]) * t3))
            out.extend([x, y])
    out.extend([pts[-1][0], pts[-1][1]])
    return out


def draw_area_chart(canvas: tk.Canvas, hourly: list):
    """Draw download + upload filled area chart for today's 24 hours."""
    canvas.delete("all")
    cw = canvas.winfo_width()
    ch = canvas.winfo_height()
    if cw < 10 or ch < 10:
        return

    pad_l, pad_r, pad_t, pad_b = 54, 12, 12, 32
    x0 = pad_l; y0 = pad_t
    w  = cw - pad_l - pad_r
    h  = ch - pad_t - pad_b

    # Background
    canvas.create_rectangle(0, 0, cw, ch, fill=BG_CHART, outline="")

    down_vals = [row[2] for row in hourly]
    up_vals   = [row[1] for row in hourly]
    max_val   = max(max(down_vals, default=0), max(up_vals, default=0)) * 1.1
    if max_val == 0:
        max_val = 1.0

    # Grid lines
    n_grid = 5
    for i in range(n_grid + 1):
        gy  = y0 + h - i * h / n_grid
        val = max_val * i / n_grid
        canvas.create_line(x0, gy, x0 + w, gy, fill="#1e293b", width=1)
        canvas.create_text(x0 - 4, gy, text=fmt_bytes(val), anchor="e",
                           fill=COL_DIM, font=("Segoe UI", 8))

    # X-axis hour labels
    tick_hours = [0, 4, 8, 12, 16, 20, 23]
    for h_idx in tick_hours:
        gx = x0 + h_idx * w / 23
        canvas.create_line(gx, y0, gx, y0 + h, fill="#1a2540", width=1)
        canvas.create_text(gx, y0 + h + 14, text=f"{h_idx:02d}:00",
                           anchor="center", fill=COL_DIM, font=("Segoe UI", 8))

    def area_fill(vals, line_color, fill_color):
        pts = _smooth_points(vals, x0, y0, w, h, max_val)
        if len(pts) < 4:
            return
        # Build polygon: spline top + bottom baseline
        poly = pts[:]
        poly.extend([x0 + w, y0 + h, x0, y0 + h])
        try:
            canvas.create_polygon(poly, fill=fill_color, outline="", smooth=False)
            canvas.create_line(pts, fill=line_color, width=2, smooth=False)
        except tk.TclError:
            pass

    # Draw download then upload
    area_fill(down_vals, COL_DOWN, "#063b4a")
    area_fill(up_vals,   COL_UP,   "#1e1b4b")

    # Axis lines
    canvas.create_line(x0, y0, x0, y0 + h, fill=COL_DIM, width=1)
    canvas.create_line(x0, y0 + h, x0 + w, y0 + h, fill=COL_DIM, width=1)


def draw_bar_chart(canvas: tk.Canvas, daily: list):
    """Draw 7-day bar chart. daily = [(date_str, upload, download, apps), ...]"""
    canvas.delete("all")
    cw = canvas.winfo_width()
    ch = canvas.winfo_height()
    if cw < 10 or ch < 10:
        return

    pad_l, pad_r, pad_t, pad_b = 54, 12, 12, 32
    x0 = pad_l; y0 = pad_t
    w  = cw - pad_l - pad_r
    h  = ch - pad_t - pad_b

    canvas.create_rectangle(0, 0, cw, ch, fill=BG_CHART, outline="")

    items = list(reversed(daily[-7:]))
    if not items:
        return
    totals = [(r[1] or 0) + (r[2] or 0) for r in items]
    max_val = max(totals, default=1) * 1.1 or 1.0

    n_grid = 4
    for i in range(n_grid + 1):
        gy  = y0 + h - i * h / n_grid
        val = max_val * i / n_grid
        canvas.create_line(x0, gy, x0 + w, gy, fill="#1e293b", width=1)
        canvas.create_text(x0 - 4, gy, text=fmt_bytes(val), anchor="e",
                           fill=COL_DIM, font=("Segoe UI", 8))

    n    = len(items)
    bw   = w / n * 0.55
    gap  = w / n

    for i, (row, tot) in enumerate(zip(items, totals)):
        cx   = x0 + gap * i + gap / 2
        bh   = (tot / max_val) * h
        by   = y0 + h - bh
        canvas.create_rectangle(cx - bw/2, by, cx + bw/2, y0 + h,
                                 fill=COL_DOWN, outline="", width=0)
        label = row[0][5:]  # MM-DD
        canvas.create_text(cx, y0 + h + 14, text=label, anchor="center",
                           fill=COL_DIM, font=("Segoe UI", 8))
        canvas.create_text(cx, by - 6, text=fmt_bytes(tot), anchor="s",
                           fill=COL_TEXT, font=("Segoe UI", 7))

    canvas.create_line(x0, y0, x0, y0 + h, fill=COL_DIM, width=1)
    canvas.create_line(x0, y0 + h, x0 + w, y0 + h, fill=COL_DIM, width=1)


# ── Custom Range Report window ────────────────────────────────────────────────

def open_range_report_window(parent, db: UsageDatabase) -> None:
    win = ctk.CTkToplevel(parent)
    win.title("ByteWatch — Custom Date Range")
    win.geometry("1000x650")
    win.minsize(880, 500)
    win.configure(fg_color=BG_MAIN)
    win.after(100, lambda: win.focus_force())

    top = ctk.CTkFrame(win, fg_color=BG_CARD, corner_radius=10)
    top.pack(fill="x", padx=16, pady=12)

    ctk.CTkLabel(top, text="Start:", font=ctk.CTkFont(size=12, weight="bold"),
                 text_color=COL_TEXT).grid(row=0, column=0, padx=(16, 4), pady=10, sticky="w")
    start_ent = ctk.CTkEntry(top, width=120, placeholder_text="YYYY-MM-DD",
                              fg_color=BG_ROW_A, border_color=COL_DIM)
    start_ent.insert(0, (date.today() - timedelta(days=7)).isoformat())
    start_ent.grid(row=0, column=1, padx=4, pady=10)

    ctk.CTkLabel(top, text="End:", font=ctk.CTkFont(size=12, weight="bold"),
                 text_color=COL_TEXT).grid(row=0, column=2, padx=(16, 4), pady=10, sticky="w")
    end_ent = ctk.CTkEntry(top, width=120, placeholder_text="YYYY-MM-DD",
                            fg_color=BG_ROW_A, border_color=COL_DIM)
    end_ent.insert(0, date.today().isoformat())
    end_ent.grid(row=0, column=3, padx=4, pady=10)

    summary_lbl = ctk.CTkLabel(win, text="Select a date span and click Analyze.",
                                font=ctk.CTkFont(size=13), text_color=COL_DIM)
    summary_lbl.pack(padx=16, pady=(2, 6), anchor="w")

    scroll = ctk.CTkScrollableFrame(win, fg_color="transparent")
    scroll.pack(fill="both", expand=True, padx=16, pady=4)

    cols = [("APPLICATION", 3), ("LOCATION", 3), ("TOTAL", 2),
            ("UPLOAD", 2), ("DOWNLOAD", 2), ("SHARE", 3)]

    tg = ctk.CTkFrame(scroll, fg_color="transparent")
    tg.pack(fill="both", expand=True)
    for i, (_, wt) in enumerate(cols):
        tg.grid_columnconfigure(i, weight=wt)

    def load_report():
        for w in tg.winfo_children():
            w.destroy()
        for i, (title, _) in enumerate(cols):
            ctk.CTkLabel(tg, text=title, anchor="w",
                         font=ctk.CTkFont(size=11, weight="bold"),
                         text_color=COL_TEXT, fg_color="#2b2b2b",
                         corner_radius=4, height=30
                         ).grid(row=0, column=i, padx=1, pady=(0, 4), sticky="nsew")

        rows = db.range_breakdown(start_ent.get().strip(), end_ent.get().strip())
        total_range = sum(r[4] for r in rows) if rows else 0
        total_up    = sum(r[2] for r in rows) if rows else 0
        total_down  = sum(r[3] for r in rows) if rows else 0
        summary_lbl.configure(
            text=(f"Span: {start_ent.get()} → {end_ent.get()}   |   "
                  f"Total: {fmt_bytes(total_range)} "
                  f"(▲ {fmt_bytes(total_up)}  •  ▼ {fmt_bytes(total_down)})   "
                  f"| {len(rows)} apps"),
            text_color=COL_TEXT)

        for r_idx, (app, exe, up, down, tot, _) in enumerate(rows, start=1):
            rbg   = BG_ROW_A if r_idx % 2 == 0 else BG_ROW_B
            share = (tot / total_range * 100) if total_range > 0 else 0.0

            ctk.CTkLabel(tg, text=format_app_name(app), anchor="w",
                         font=ctk.CTkFont(size=12, weight="bold"),
                         fg_color=rbg, corner_radius=0
                         ).grid(row=r_idx, column=0, padx=1, pady=1, sticky="nsew")

            folder = os.path.dirname(exe) if exe else "Unknown"
            disp   = folder if len(folder) <= 24 else folder[:11] + "..." + folder[-11:]
            ll     = ctk.CTkLabel(tg, text=disp, anchor="w",
                                   font=ctk.CTkFont(size=11, underline=True),
                                   text_color="#60a5fa", fg_color=rbg,
                                   corner_radius=0, cursor="hand2")
            ll.grid(row=r_idx, column=1, padx=1, pady=1, sticky="nsew")
            ll.bind("<Button-1>", lambda e, p=exe: open_folder(p))

            for col_i, (text, color) in enumerate([
                (fmt_bytes(tot), COL_TEXT),
                (fmt_bytes(up),  COL_UP),
                (fmt_bytes(down), COL_DOWN),
            ], start=2):
                ctk.CTkLabel(tg, text=text, anchor="w",
                             font=ctk.CTkFont(size=12), text_color=color,
                             fg_color=rbg, corner_radius=0
                             ).grid(row=r_idx, column=col_i, padx=1, pady=1, sticky="nsew")

            sc = ctk.CTkFrame(tg, fg_color=rbg, corner_radius=0)
            sc.grid(row=r_idx, column=5, padx=1, pady=1, sticky="nsew")
            pb = ctk.CTkProgressBar(sc, height=8, corner_radius=4,
                                     progress_color=COL_DOWN)
            pb.set(share / 100.0)
            pb.pack(side="left", fill="x", expand=True, padx=(8, 4), pady=6)
            ctk.CTkLabel(sc, text=f"{share:4.1f}%",
                         font=ctk.CTkFont(size=10, weight="bold"),
                         width=44, anchor="e").pack(side="right", padx=(0, 4))

    ctk.CTkButton(top, text="Analyze", width=110, height=32,
                  fg_color="#1d4ed8", hover_color="#2563eb",
                  command=load_report).grid(row=0, column=4, padx=16, pady=10)
    load_report()


# ── Detail Window (single day) ────────────────────────────────────────────────

def open_detail_window(parent, db: UsageDatabase, day: date) -> None:
    win = ctk.CTkToplevel(parent)
    win.title(f"ByteWatch — {day.isoformat()}")
    win.geometry("1020x640")
    win.minsize(880, 480)
    win.configure(fg_color=BG_MAIN)
    win.after(100, lambda: win.focus_force())

    rows  = db.day_breakdown(day)
    total = sum(r[4] for r in rows) if rows else 0
    t_up  = sum(r[2] for r in rows) if rows else 0
    t_dn  = sum(r[3] for r in rows) if rows else 0

    # Header card
    hdr = ctk.CTkFrame(win, fg_color=BG_CARD, corner_radius=10)
    hdr.pack(fill="x", padx=16, pady=(14, 6))

    ctk.CTkLabel(hdr, text=day.strftime("%A, %B %d, %Y"),
                 font=ctk.CTkFont(size=16, weight="bold"),
                 text_color=COL_TEXT).pack(side="left", padx=16, pady=12)

    for txt, col in [
        (f"▼ {fmt_bytes(t_dn)}", COL_DOWN),
        (f"▲ {fmt_bytes(t_up)}", COL_UP),
        (f"Total: {fmt_bytes(total)}", COL_TEXT),
        (f"{len(rows)} apps", COL_DIM),
    ]:
        ctk.CTkLabel(hdr, text=txt, font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=col).pack(side="left", padx=14, pady=12)

    # Table
    scroll = ctk.CTkScrollableFrame(win, fg_color="transparent")
    scroll.pack(fill="both", expand=True, padx=16, pady=(0, 8))

    cols = [("APPLICATION", 3), ("LOCATION", 3), ("TOTAL", 2),
            ("UPLOAD", 2), ("DOWNLOAD", 2), ("SHARE", 3)]

    tg = ctk.CTkFrame(scroll, fg_color="transparent")
    tg.pack(fill="both", expand=True)
    for i, (_, wt) in enumerate(cols):
        tg.grid_columnconfigure(i, weight=wt)

    for i, (title, _) in enumerate(cols):
        ctk.CTkLabel(tg, text=title, anchor="w",
                     font=ctk.CTkFont(size=11, weight="bold"),
                     text_color=COL_TEXT, fg_color="#2b2b2b",
                     corner_radius=4, height=30
                     ).grid(row=0, column=i, padx=1, pady=(0, 4), sticky="nsew")

    for r_idx, (app, exe, up, down, tot, _) in enumerate(rows, start=1):
        rbg   = BG_ROW_A if r_idx % 2 == 0 else BG_ROW_B
        share = (tot / total * 100) if total > 0 else 0.0

        ctk.CTkLabel(tg, text=format_app_name(app), anchor="w",
                     font=ctk.CTkFont(size=12, weight="bold"),
                     fg_color=rbg, corner_radius=0
                     ).grid(row=r_idx, column=0, padx=1, pady=1, sticky="nsew")

        folder = os.path.dirname(exe) if exe else "Unknown"
        disp   = folder if len(folder) <= 24 else folder[:11] + "..." + folder[-11:]
        ll     = ctk.CTkLabel(tg, text=disp, anchor="w",
                               font=ctk.CTkFont(size=11, underline=True),
                               text_color="#60a5fa", fg_color=rbg,
                               corner_radius=0, cursor="hand2")
        ll.grid(row=r_idx, column=1, padx=1, pady=1, sticky="nsew")
        ll.bind("<Button-1>", lambda e, p=exe: open_folder(p))

        for col_i, (text, color) in enumerate([
            (fmt_bytes(tot), COL_TEXT),
            (fmt_bytes(up),  COL_UP),
            (fmt_bytes(down), COL_DOWN),
        ], start=2):
            ctk.CTkLabel(tg, text=text, anchor="w",
                         font=ctk.CTkFont(size=12), text_color=color,
                         fg_color=rbg, corner_radius=0
                         ).grid(row=r_idx, column=col_i, padx=1, pady=1, sticky="nsew")

        sc = ctk.CTkFrame(tg, fg_color=rbg, corner_radius=0)
        sc.grid(row=r_idx, column=5, padx=1, pady=1, sticky="nsew")
        pb = ctk.CTkProgressBar(sc, height=8, corner_radius=4,
                                 progress_color=COL_DOWN)
        pb.set(share / 100.0)
        pb.pack(side="left", fill="x", expand=True, padx=(8, 4), pady=6)
        ctk.CTkLabel(sc, text=f"{share:4.1f}%",
                     font=ctk.CTkFont(size=10, weight="bold"),
                     width=44, anchor="e").pack(side="right", padx=(0, 4))

    ctk.CTkButton(win, text="Close", width=100, height=30,
                  fg_color=BG_CARD, hover_color=BG_ROW_A,
                  command=win.destroy).pack(pady=(4, 10))


# ── Main window ───────────────────────────────────────────────────────────────

def build_main_window(db: UsageDatabase, collector: UsageCollector) -> ctk.CTk:
    root = ctk.CTk()
    root.title("ByteWatch")
    root.geometry("1100x740")
    root.minsize(900, 580)
    root.configure(fg_color=BG_MAIN)

    sort_column    = {"key": "date", "reverse": True}
    days_limit_var = tk.StringVar(value="30")
    chart_mode     = {"mode": "activity"}   # "activity" | "trend"
    _pending_refresh = {"id": None}         # debounce handle

    # ── Live speed state (updated by background thread) ────────────────────
    _speed = {"up": 0.0, "down": 0.0}
    _last_net = {"c": None}

    def _speed_loop():
        """Runs in a daemon thread – reads psutil every 2 s, posts to main thread."""
        while True:
            time.sleep(2)
            try:
                cur = psutil.net_io_counters()
                if _last_net["c"] is not None:
                    prev = _last_net["c"]
                    _speed["up"]   = max(0.0, (cur.bytes_sent - prev.bytes_sent) / 2.0)
                    _speed["down"] = max(0.0, (cur.bytes_recv - prev.bytes_recv) / 2.0)
                _last_net["c"] = cur
                root.after(0, _update_speed_label)
            except Exception:
                pass

    # ══════════════════════════════════════════════════════════════════════════
    # HEADER BAR
    # ══════════════════════════════════════════════════════════════════════════
    header = ctk.CTkFrame(root, fg_color=BG_HEADER, corner_radius=0, height=46)
    header.pack(fill="x", side="top")
    header.pack_propagate(False)

    # Left: app brand
    brand = ctk.CTkFrame(header, fg_color="transparent")
    brand.pack(side="left", padx=(14, 0))
    ctk.CTkLabel(brand, text="⚡", font=ctk.CTkFont(size=16),
                 text_color="#38bdf8").pack(side="left")
    ctk.CTkLabel(brand, text=" ByteWatch",
                 font=ctk.CTkFont(size=15, weight="bold"),
                 text_color=COL_TEXT).pack(side="left")
    ctk.CTkLabel(brand, text=" by Yossof ",
                 font=ctk.CTkFont(size=10),
                 text_color=COL_DIM).pack(side="left", padx=(2, 0))

    # Centre: live speed readout
    speed_frame = ctk.CTkFrame(header, fg_color="#0d1b2a", corner_radius=8)
    speed_frame.place(relx=0.5, rely=0.5, anchor="center")
    speed_lbl = ctk.CTkLabel(speed_frame,
                              text="▼ 0 B/s   ▲ 0 B/s",
                              font=ctk.CTkFont(size=12, weight="bold"),
                              text_color=COL_DOWN)
    speed_lbl.pack(padx=14, pady=5)

    def _update_speed_label():
        speed_lbl.configure(
            text=f"▼ {fmt_speed(_speed['down'])}   ▲ {fmt_speed(_speed['up'])}")

    # Right: control buttons
    ctrl = ctk.CTkFrame(header, fg_color="transparent")
    ctrl.pack(side="right", padx=14)

    def _btn(parent, text, cmd, w=90):
        return ctk.CTkButton(parent, text=text, width=w, height=28,
                             fg_color=COL_BTN, hover_color=COL_BTN_H,
                             font=ctk.CTkFont(size=11),
                             corner_radius=6, command=cmd)

    _btn(ctrl, "🔄 Refresh",   lambda: schedule_refresh(), w=88).pack(side="left", padx=4)
    _btn(ctrl, "⚡ Aggregate", lambda: force_aggregate(), w=100).pack(side="left", padx=4)

    startup_var = tk.BooleanVar(value=is_startup_enabled())
    startup_chk = ctk.CTkCheckBox(ctrl, text="Startup",
                                   variable=startup_var,
                                   font=ctk.CTkFont(size=11),
                                   command=lambda: set_startup(startup_var.get()),
                                   width=80)
    startup_chk.pack(side="left", padx=8)

    # ══════════════════════════════════════════════════════════════════════════
    # KPI CARDS
    # ══════════════════════════════════════════════════════════════════════════
    kpi_frame = ctk.CTkFrame(root, fg_color="transparent")
    kpi_frame.pack(fill="x", padx=14, pady=(10, 6))
    kpi_frame.grid_columnconfigure((0, 1, 2, 3), weight=1)

    def make_card(parent, col, title):
        c = ctk.CTkFrame(parent, fg_color=BG_CARD, corner_radius=10)
        c.grid(row=0, column=col, padx=5, pady=4, sticky="nsew")
        ctk.CTkLabel(c, text=title, font=ctk.CTkFont(size=10, weight="bold"),
                     text_color=COL_DIM).pack(anchor="w", padx=12, pady=(10, 1))
        return c

    c1 = make_card(kpi_frame, 0, "TODAY'S TOTAL")
    c1_val = ctk.CTkLabel(c1, text="0.00 B",
                           font=ctk.CTkFont(size=20, weight="bold"), text_color=COL_TEXT)
    c1_val.pack(anchor="w", padx=12)
    c1_sub = ctk.CTkLabel(c1, text="Live calculated",
                           font=ctk.CTkFont(size=10), text_color=COL_DIM)
    c1_sub.pack(anchor="w", padx=12, pady=(1, 10))

    c2 = make_card(kpi_frame, 1, "DOWN / UP STREAM")
    c2_val = ctk.CTkLabel(c2, text="▼ 0 B   ▲ 0 B",
                           font=ctk.CTkFont(size=14, weight="bold"), text_color=COL_ACCENT)
    c2_val.pack(anchor="w", padx=12)
    ctk.CTkLabel(c2, text="Today's stream",
                 font=ctk.CTkFont(size=10), text_color=COL_DIM).pack(anchor="w", padx=12, pady=(1, 10))

    c3 = make_card(kpi_frame, 2, "TOP CONSUMER")
    c3_val = ctk.CTkLabel(c3, text="None",
                           font=ctk.CTkFont(size=15, weight="bold"), text_color=COL_TEXT)
    c3_val.pack(anchor="w", padx=12)
    c3_sub = ctk.CTkLabel(c3, text="0% of today's traffic",
                           font=ctk.CTkFont(size=10), text_color=COL_DIM)
    c3_sub.pack(anchor="w", padx=12, pady=(1, 10))

    c4 = make_card(kpi_frame, 3, "MONTH TO DATE")
    c4_val = ctk.CTkLabel(c4, text="0 B / 0 B",
                           font=ctk.CTkFont(size=15, weight="bold"), text_color=COL_TEXT)
    c4_val.pack(anchor="w", padx=12)
    c4_sub = ctk.CTkLabel(c4, text="Current / avg monthly",
                           font=ctk.CTkFont(size=10), text_color=COL_DIM)
    c4_sub.pack(anchor="w", padx=12, pady=(1, 10))

    # ══════════════════════════════════════════════════════════════════════════
    # VISUAL ANALYTICS SECTION
    # ══════════════════════════════════════════════════════════════════════════
    va_outer = ctk.CTkFrame(root, fg_color=BG_CARD, corner_radius=10)
    va_outer.pack(fill="x", padx=14, pady=(0, 8))

    va_top = ctk.CTkFrame(va_outer, fg_color="transparent")
    va_top.pack(fill="x", padx=12, pady=(8, 4))

    ctk.CTkLabel(va_top, text="Visual Analytics",
                 font=ctk.CTkFont(size=13, weight="bold"),
                 text_color=COL_TEXT).pack(side="left")

    # Legend
    leg = ctk.CTkFrame(va_top, fg_color="transparent")
    leg.pack(side="left", padx=20)
    ctk.CTkLabel(leg, text="●", text_color=COL_DOWN,
                 font=ctk.CTkFont(size=12)).pack(side="left", padx=(0, 2))
    ctk.CTkLabel(leg, text="Download (▼)",
                 font=ctk.CTkFont(size=11), text_color=COL_DIM).pack(side="left", padx=(0, 10))
    ctk.CTkLabel(leg, text="●", text_color=COL_UP,
                 font=ctk.CTkFont(size=12)).pack(side="left", padx=(0, 2))
    ctk.CTkLabel(leg, text="Upload (▲)",
                 font=ctk.CTkFont(size=11), text_color=COL_DIM).pack(side="left")

    # Toggle buttons (right side)
    toggle_box = ctk.CTkFrame(va_top, fg_color="transparent")
    toggle_box.pack(side="right")

    btn_activity = ctk.CTkButton(toggle_box, text="● Network Activity", width=130, height=26,
                                  font=ctk.CTkFont(size=11),
                                  fg_color="#1d4ed8", hover_color="#2563eb",
                                  corner_radius=6, command=lambda: switch_chart("activity"))
    btn_activity.pack(side="left", padx=4)

    btn_trend = ctk.CTkButton(toggle_box, text="● 7-Day Trend", width=110, height=26,
                               font=ctk.CTkFont(size=11),
                               fg_color=COL_BTN, hover_color=COL_BTN_H,
                               corner_radius=6, command=lambda: switch_chart("trend"))
    btn_trend.pack(side="left", padx=4)

    # Canvas
    chart_canvas = tk.Canvas(va_outer, height=175, bg=BG_CHART,
                              highlightthickness=0, bd=0)
    chart_canvas.pack(fill="x", padx=8, pady=(0, 10))

    _chart_data = {"hourly": None, "daily": None}

    def switch_chart(mode: str):
        chart_mode["mode"] = mode
        if mode == "activity":
            btn_activity.configure(fg_color="#1d4ed8")
            btn_trend.configure(fg_color=COL_BTN)
            _redraw_chart()
        else:
            btn_activity.configure(fg_color=COL_BTN)
            btn_trend.configure(fg_color="#1d4ed8")
            _redraw_chart()

    def _redraw_chart():
        if chart_mode["mode"] == "activity":
            data = _chart_data["hourly"] or [(h, 0.0, 0.0) for h in range(24)]
            draw_area_chart(chart_canvas, data)
        else:
            data = _chart_data["daily"] or []
            draw_bar_chart(chart_canvas, data)

    def _refresh_chart_bg():
        """Fetch chart data on background thread, then redraw."""
        def _fetch():
            try:
                hourly = db.hourly_today()
            except Exception:
                hourly = [(h, 0.0, 0.0) for h in range(24)]
            root.after(0, lambda: _apply_chart(hourly))
        threading.Thread(target=_fetch, daemon=True).start()

    def _apply_chart(hourly):
        _chart_data["hourly"] = hourly
        _redraw_chart()

    chart_canvas.bind("<Configure>", lambda e: root.after(50, _redraw_chart))

    # ══════════════════════════════════════════════════════════════════════════
    # DAILY LOG HISTORY HEADER
    # ══════════════════════════════════════════════════════════════════════════
    log_header = ctk.CTkFrame(root, fg_color="transparent")
    log_header.pack(fill="x", padx=14, pady=(2, 4))

    ctk.CTkLabel(log_header, text="Daily Log History",
                 font=ctk.CTkFont(size=14, weight="bold"),
                 text_color=COL_TEXT).pack(side="left")

    # Right controls
    right_ctrl = ctk.CTkFrame(log_header, fg_color="transparent")
    right_ctrl.pack(side="right")

    _btn(right_ctrl, "📅 Custom Span",
         lambda: open_range_report_window(root, db), w=108).pack(side="left", padx=3)

    _btn(right_ctrl, "📤 Export CSV",
         lambda: export_csv(), w=96).pack(side="left", padx=3)

    ctk.CTkFrame(right_ctrl, fg_color=COL_DIM, width=1, height=22,
                 corner_radius=0).pack(side="left", padx=6)

    for label, val in [("7D", "7"), ("14D", "14"), ("30D", "30"), ("All", "")]:
        def _preset(v=val):
            days_limit_var.set(v)
            schedule_refresh()
        ctk.CTkButton(right_ctrl, text=label, width=34, height=26,
                      fg_color=COL_BTN, hover_color=COL_BTN_H,
                      font=ctk.CTkFont(size=11), corner_radius=5,
                      command=_preset).pack(side="left", padx=2)

    # ══════════════════════════════════════════════════════════════════════════
    # MAIN TABLE
    # ══════════════════════════════════════════════════════════════════════════
    scroll = ctk.CTkScrollableFrame(root, fg_color="transparent")
    scroll.pack(fill="both", expand=True, padx=14, pady=(0, 10))

    main_cols = [
        ("Date",        "date",     3),
        ("Upload",      "upload",   2),
        ("Download",    "download", 2),
        ("Total",       "total",    2),
        ("Active Apps", "apps",     2),
    ]

    tg = ctk.CTkFrame(scroll, fg_color="transparent")
    tg.pack(fill="both", expand=True)
    for i, (_, _, wt) in enumerate(main_cols):
        tg.grid_columnconfigure(i, weight=wt)

    # ── Background data fetch ─────────────────────────────────────────────
    _table_cache = {"data": []}

    def _fetch_and_render():
        limit_val = None
        raw = days_limit_var.get().strip()
        if raw.isdigit() and int(raw) > 0:
            limit_val = int(raw)

        def _bg():
            try:
                data   = db.daily_totals(limit_days=limit_val)
                mtd    = db.monthly_stats()
                today  = date.today().isoformat()
                today_row = next((r for r in data if r[0] == today), None)
                breakdown = db.day_breakdown(date.today()) if today_row else []
            except Exception:
                data, mtd, today_row, breakdown = [], (0, 0), None, []
            root.after(0, lambda: _apply_data(data, mtd, today_row, breakdown))

        threading.Thread(target=_bg, daemon=True).start()

    def _apply_data(data, mtd, today_row, breakdown):
        # Update KPI cards
        if today_row:
            t_up, t_dn = today_row[1], today_row[2]
            t_tot = t_up + t_dn
            c1_val.configure(text=fmt_bytes(t_tot))
            c2_val.configure(text=f"▼ {fmt_bytes(t_dn)}   ▲ {fmt_bytes(t_up)}")
            if breakdown:
                top  = breakdown[0]
                share = (top[4] / t_tot * 100) if t_tot > 0 else 0.0
                c3_val.configure(text=format_app_name(top[0], limit=18))
                c3_sub.configure(text=f"{share:.1f}% of today's traffic")
            else:
                c3_val.configure(text="None")
                c3_sub.configure(text="0% of traffic")
        else:
            c1_val.configure(text="0.00 B")
            c2_val.configure(text="▼ 0 B   ▲ 0 B")
            c3_val.configure(text="Idle")
            c3_sub.configure(text="Awaiting data")

        cur_m, avg_m = mtd
        c4_val.configure(text=f"{fmt_bytes(cur_m)} / {fmt_bytes(avg_m)}")

        # Update 7-day trend chart data
        _chart_data["daily"] = data
        if chart_mode["mode"] == "trend":
            _redraw_chart()

        # Render table
        _table_cache["data"] = data
        _render_table(data)

    def _render_table(data):
        for w in tg.winfo_children():
            w.destroy()

        # Sortable headers
        for i, (title, key, _) in enumerate(main_cols):
            arrow = (" ▼" if sort_column["reverse"] else " ▲") if sort_column["key"] == key else " ↕"
            ctk.CTkButton(
                tg, text=f"{title}{arrow}",
                font=ctk.CTkFont(size=12, weight="bold"),
                fg_color="#252d3d", hover_color="#2d3f56",
                anchor="w", height=30, corner_radius=4,
                command=lambda k=key: _sort_and_render(k),
            ).grid(row=0, column=i, padx=1, pady=(0, 4), sticky="nsew")

        def _sort_key(item):
            d, up, dn, apps = item
            tot = (up or 0) + (dn or 0)
            return {
                "date": d, "upload": up or 0, "download": dn or 0,
                "total": tot, "apps": apps or 0,
            }.get(sort_column["key"], d)

        sorted_data = sorted(data, key=_sort_key, reverse=sort_column["reverse"])

        for r_idx, (day_str, up, dn, app_count) in enumerate(sorted_data, start=1):
            rbg  = BG_ROW_A if r_idx % 2 == 0 else BG_ROW_B
            tot  = (up or 0) + (dn or 0)
            vals = [day_str, fmt_bytes(up), fmt_bytes(dn), fmt_bytes(tot), str(app_count)]
            clrs = [None, COL_UP, COL_DOWN, None, None]

            for i, (val, col) in enumerate(zip(vals, clrs)):
                lbl = ctk.CTkLabel(
                    tg, text=val, anchor="w",
                    font=ctk.CTkFont(size=13),
                    text_color=col if col else COL_TEXT,
                    fg_color=rbg, corner_radius=0,
                    cursor="hand2", height=32,
                )
                lbl.grid(row=r_idx, column=i, padx=1, pady=1, sticky="nsew")
                lbl.bind("<Button-1>", lambda e, d=day_str: _open_day(d))

    def _sort_and_render(key):
        if sort_column["key"] == key:
            sort_column["reverse"] = not sort_column["reverse"]
        else:
            sort_column["key"]     = key
            sort_column["reverse"] = True
        _render_table(_table_cache["data"])

    def _open_day(day_str: str):
        y, m, d = (int(x) for x in day_str.split("-"))
        open_detail_window(root, db, date(y, m, d))

    # ── Debounced refresh ─────────────────────────────────────────────────
    def schedule_refresh(delay_ms: int = 0):
        """Cancel any pending refresh and schedule a fresh one."""
        if _pending_refresh["id"] is not None:
            root.after_cancel(_pending_refresh["id"])
        _pending_refresh["id"] = root.after(delay_ms, _do_refresh)

    def _do_refresh():
        _pending_refresh["id"] = None
        _fetch_and_render()
        _refresh_chart_bg()

    def force_aggregate():
        collector.collect()
        schedule_refresh()

    # ── CSV export ────────────────────────────────────────────────────────
    def export_csv():
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialfile=f"bytewatch_{date.today().isoformat()}.csv",
            title="Export Daily Log to CSV",
        )
        if not path:
            return
        data = _table_cache["data"]
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["Date", "Upload (bytes)", "Download (bytes)",
                             "Total (bytes)", "Active Apps"])
                for day_str, up, dn, apps in data:
                    w.writerow([day_str, up or 0, dn or 0,
                                 (up or 0) + (dn or 0), apps])
        except Exception as e:
            print(f"CSV export error: {e}")

    # ── Tray / window management ──────────────────────────────────────────
    def show_window(icon=None, item=None):
        root.after(0, _restore_window)

    def _restore_window():
        root.deiconify()
        root.state("normal")
        root.lift()
        root.attributes("-topmost", True)
        root.after_idle(root.attributes, "-topmost", False)
        root.focus_force()
        schedule_refresh()

    def hide_window():
        root.withdraw()

    def quit_application(icon=None, item=None):
        collector.stop()
        if hasattr(root, "tray_icon") and root.tray_icon:
            try:
                root.tray_icon.stop()
            except Exception:
                pass
        root.after(0, root.destroy)
        os._exit(0)

    root.protocol("WM_DELETE_WINDOW", hide_window)

    # ── Auto-refresh every 10 s (off main thread) ─────────────────────────
    def _auto_refresh():
        schedule_refresh()
        root.after(10_000, _auto_refresh)

    root.after(10_000, _auto_refresh)

    # ── Tray setup ────────────────────────────────────────────────────────
    tray_menu = pystray.Menu(
        pystray.MenuItem("Open Dashboard", show_window, default=True),
        pystray.MenuItem("Aggregate Now", lambda *a: root.after(0, force_aggregate)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit Completely", quit_application),
    )
    tray_icon        = pystray.Icon("ByteWatch", create_tray_image(),
                                    "ByteWatch Tracker", tray_menu)
    root.tray_icon   = tray_icon
    threading.Thread(target=tray_icon.run, daemon=True).start()

    # ── Start speed thread and initial load ───────────────────────────────
    threading.Thread(target=_speed_loop, daemon=True).start()
    schedule_refresh()   # first draw

    return root


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    base_dir  = os.path.dirname(os.path.abspath(__file__))
    db        = UsageDatabase(get_db_path(base_dir))
    collector = UsageCollector(db, interval_seconds=15, retention_days=180)
    collector.start()

    root = build_main_window(db, collector)

    if "--tray" in sys.argv or "--minimized" in sys.argv:
        root.withdraw()

    root.mainloop()


if __name__ == "__main__":
    main()
