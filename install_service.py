"""
SoCandyShop TikTok Backend — systemd service setup

This script will:
1. Create a systemd user service for the TikTok backend
2. Set the TIKTOK_MS_TOKEN env var (optional)
3. Enable auto-start

The backend runs on port 3100 (matching the existing proxy URL).
"""

import os
import sys

SERVICE_NAME = "nanabot"
SERVICE_FILE = os.path.expanduser(f"~/.config/systemd/user/{SERVICE_NAME}.service")

SERVICE_CONTENT = f"""\
[Unit]
Description=SoCandyShop TikTok Backend (FastAPI + TikTok-Api)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/xvfb-run --auto-servernum {os.path.expanduser("~/nanabot/venv/bin/python")} {os.path.expanduser("~/nanabot/main.py")}
WorkingDirectory={os.path.expanduser("~/nanabot")}
Restart=on-failure
RestartSec=10
Environment=HOST=0.0.0.0
Environment=PORT=3100
Environment=TIKTOK_SESSIONS=2
Environment=TIKTOK_HEADLESS=true
Environment=LIVE_CHECK_INTERVAL=60
Environment=TIKTOK_SHOP_HANDLE=soetsopains
Environment=DISPLAY=:99
# Uncomment and set your ms_token below:
# Environment=TIKTOK_MS_TOKEN=your_ms_token_here

[Install]
WantedBy=default.target
"""


def main():
    print(f"Creating service: {SERVICE_FILE}")
    os.makedirs(os.path.dirname(SERVICE_FILE), exist_ok=True)
    with open(SERVICE_FILE, "w") as f:
        f.write(SERVICE_CONTENT)
    print("Service file written.")

    # Enable linger so the service runs on boot (even without login)
    os.system("loginctl enable-linger $(whoami) 2>/dev/null || true")
    os.system(f"systemctl --user daemon-reload 2>/dev/null || true")
    print("Done! To start the service:")
    print(f"  systemctl --user start {SERVICE_NAME}")
    print(f"  systemctl --user enable {SERVICE_NAME}")
    print(f"  systemctl --user status {SERVICE_NAME}")
    print(f"  journalctl --user -u {SERVICE_NAME} -f")


if __name__ == "__main__":
    main()
