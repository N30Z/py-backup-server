# Mini Backup Server

Ein kleiner Python-Webserver zur Verwaltung automatischer Backups unter Linux.
Er bietet eine einfache Weboberfläche, um Backup-Jobs mit Quelle, Ziel und Cron-Zeitplan anzulegen.

## Features
- Weboberfläche und REST-API
- Mehrere Backup-Jobs mit Quelle/Ziel und Cron-Zeitplan
- Nur Änderungen werden kopiert (rsync --dry-run)
- Automatische und manuelle Ausführung
- JSON-Speicher, Logfiles, Systemd-Integration

## Installation

```bash
sudo apt install python3-venv rsync
git clone https://github.com/YOURNAME/mini-backup-server.git
cd mini-backup-server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
HOST=127.0.0.1 PORT=8000 BACKUP_SERVER_DATA=./data python app.py
```

Danach: http://127.0.0.1:8000 öffnen.
