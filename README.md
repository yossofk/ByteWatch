# ByteWatch

ByteWatch is a lightweight Windows system tray utility built with Python that monitors real-time internet bandwidth consumption and tracks daily data usage per executable.

## Features
- **System Tray Integration:** Runs quietly in the background without cluttering the taskbar (`app.pyw`).
- **Per-Application Tracking:** Monitors upload and download bandwidth broken down by individual Windows executables.
- **Persistent Local Storage:** Logs historical daily usage locally using SQLite (`data_layer.py`).
- **One-Click Launch:** Includes a Windows batch launcher (`run.bat`) for quick startup.

## Project Structure
- `app.pyw` — Main GUI and system tray application (runs without opening a console window).
- `data_layer.py` — SQLite database management and network metrics aggregation.
- `run.bat` — Windows batch script to launch the application.
- `requirements.txt` — Required Python packages.

## Installation & Usage

1. **Clone or download the repository:**
   ```bash
   git clone [https://github.com/yossofk/ByteWatch.git](https://github.com/yossofk/ByteWatch.git)
   cd bytewatch