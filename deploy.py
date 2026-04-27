"""Script to deploy the TikTok backend to socandyshopfr server."""
import subprocess, sys, os

# Override with env var or pass as second arg: python deploy.py send user@host
SERVER = os.getenv("TIKTOK_SERVER", "socandyshopfr")
if len(sys.argv) >= 3:
    SERVER = sys.argv[2]
    del sys.argv[1]  # Remove from argv so action is still at index 1

DIR = "/home/ekah/tiktok-backend"
VENV_PYTHON = f"{DIR}/venv/bin/python"
SERVICE_NAME = "socandyshop-tiktok"

def run(cmd, check=True):
    print(f"$ {cmd}")
    r = subprocess.run(cmd, shell=True)
    if check and r.returncode != 0:
        print(f"  FAILED (exit {r.returncode})")
        sys.exit(1)

if len(sys.argv) < 2:
    print("Usage: python deploy.py <action>")
    print("  send       — rsync code to server")
    print("  install    — install deps + playwright on server")
    print("  start      — start the service")
    print("  stop       — stop the service")
    print("  logs       — view logs")
    print("  status     — check status")
    print("  setup      — send + install + start (full deploy)")
    print("  check-tiktok  — run a quick TikTok API test on server")
    sys.exit(0)

action = sys.argv[1]

PWD = os.path.dirname(os.path.abspath(__file__))

if action in ("send", "setup"):
    # Rsync code (exclude venv, pycache)
    run(f"rsync -avz --exclude venv --exclude __pycache__ --exclude '.git' {PWD}/ {SERVER}:{DIR}/")

if action in ("install", "setup"):
    run(f"ssh {SERVER} 'cd {DIR} && {VENV_PYTHON} -m pip install -q fastapi uvicorn httpx TikTokApi 2>&1 | tail -3'")
    run(f"ssh {SERVER} 'cd {DIR} && {VENV_PYTHON} -m playwright install chromium 2>&1 | tail -3'")
    # Install service
    run(f"ssh {SERVER} 'cd {DIR} && {VENV_PYTHON} install_service.py'")

if action in ("start", "setup"):
    run(f"ssh {SERVER} 'systemctl --user daemon-reload && systemctl --user start {SERVICE_NAME}'", check=False)
    run(f"ssh {SERVER} 'systemctl --user enable {SERVICE_NAME}'", check=False)
    print("Waiting 5s for startup...")
    run(f"ssh {SERVER} 'sleep 5 && curl -s http://localhost:3100/health'")

if action == "stop":
    run(f"ssh {SERVER} 'systemctl --user stop {SERVICE_NAME}'")

if action == "status":
    run(f"ssh {SERVER} 'systemctl --user status {SERVICE_NAME}'")

if action == "logs":
    run(f"ssh {SERVER} 'journalctl --user -u {SERVICE_NAME} -n 50 --no-pager -f'")

if action == "check-tiktok":
    remote_script = '''cd {DIR} && {VENV_PYTHON} -c "
import asyncio, sys
sys.path.insert(0, '{DIR}')
from TikTokApi import TikTokApi
async def test():
    api = TikTokApi()
    await api.create_sessions(num_sessions=1, headless=True, sleep_after=3)
    print('Sessions created OK')
    user = api.user('soetsopains')
    info = await user.info()
    user_info = info.get('userInfo', {}).get('user', {})
    print(f'User: {user_info.get(\"uniqueId\", \"?\")}')
    await api.close_sessions()
    print('Cleanup OK')
asyncio.run(test())
"'''
    run(f'ssh {SERVER} \'{remote_script}\' 2>&1')
