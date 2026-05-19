import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import time
import os
import requests
import threading
import subprocess
import tempfile
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from parser import parse_sii
from dotenv import load_dotenv
import pystray
from PIL import Image, ImageDraw
from dispatcher import generate_and_play, build_dispatch_messages
from telemetry import start_telemetry_loop

load_dotenv()

SERVER_URL = os.getenv('SERVER_URL', 'http://127.0.0.1:5001')
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN', '')
DISCORD_ID = os.getenv('DISCORD_ID', '')
DISCORD_USERNAME = os.getenv('DISCORD_USERNAME', '')
SAVE_PATH = os.getenv('SAVE_PATH', '')

last_snapshot = {}

def find_save_file():
    steam_userdata = os.path.expandvars(r'%PROGRAMFILES(X86)%\Steam\userdata')
    if not os.path.exists(steam_userdata):
        return None
    for user_id in os.listdir(steam_userdata):
        ats_path = os.path.join(steam_userdata, user_id, '270880', 'remote', 'profiles')
        if os.path.exists(ats_path):
            profiles = sorted(os.listdir(ats_path), key=lambda p: os.path.getmtime(os.path.join(ats_path, p)), reverse=True)
            for profile in profiles:
                save_dir = os.path.join(ats_path, profile, 'save')
                if os.path.exists(save_dir):
                    saves = [s for s in os.listdir(save_dir) if os.path.isdir(os.path.join(save_dir, s))]
                    for save in sorted(saves, key=lambda s: os.path.getmtime(os.path.join(save_dir, s)), reverse=True):
                        candidate = os.path.join(save_dir, save, 'game.sii')
                        if os.path.exists(candidate):
                            return candidate
    return None

def decrypt_save(filepath):
    decrypt_exe = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'SII_Decrypt.exe')
    if not os.path.exists(decrypt_exe):
        print('[Dispatch] SII_Decrypt.exe not found in client folder')
        return None

    tmp = tempfile.NamedTemporaryFile(suffix='.sii', delete=False)
    tmp.close()

    try:
        result = subprocess.run(
            [decrypt_exe, filepath, tmp.name],
            capture_output=True,
            timeout=60
        )
        if result.returncode == 0:
            return tmp.name
        else:
            print(f'[Dispatch] Decryption failed: {result.stderr.decode()}')
            return None
    except Exception as e:
        print(f'[Dispatch] Decryption error: {e}')
        return None

def push_data(filepath):
    global last_snapshot

    decrypted = decrypt_save(filepath)
    if not decrypted:
        print('[Dispatch] Skipping push — could not decrypt save')
        return

    try:
        data = parse_sii(decrypted)

        messages = build_dispatch_messages(last_snapshot, data)
        for msg in messages:
            print(f'[Dispatch] 📻 {msg}')
            generate_and_play(msg)

        last_snapshot = data

        response = requests.post(
            f'{SERVER_URL}/api/snapshot',
            json=data,
            headers={
                'Authorization': f'Bearer {DISCORD_TOKEN}',
                'X-Discord-ID': DISCORD_ID,
                'X-Discord-Username': DISCORD_USERNAME,
            },
            timeout=10
        )
        if response.status_code == 200:
            print(f'[Dispatch] Snapshot pushed successfully.')
        else:
            print(f'[Dispatch] Server returned {response.status_code}')
    except Exception as e:
        print(f'[Dispatch] Failed to push: {e}')
    finally:
        if os.path.exists(decrypted):
            os.unlink(decrypted)

class SaveWatcher(FileSystemEventHandler):
    def __init__(self, filepath):
        self.filepath = filepath
        self.last_push = 0

    def on_modified(self, event):
        if event.src_path.endswith('game.sii'):
            now = time.time()
            if now - self.last_push > 30:
                self.last_push = now
                print('[Dispatch] Save detected, pushing...')
                push_data(self.filepath)

def create_tray_icon():
    img = Image.new('RGB', (64, 64), color=(15, 17, 23))
    draw = ImageDraw.Draw(img)
    draw.rectangle([16, 16, 48, 48], fill=(245, 166, 35))
    return img

def start_watcher(filepath):
    event_handler = SaveWatcher(filepath)
    observer = Observer()
    observer.schedule(event_handler, path=os.path.dirname(filepath), recursive=False)
    observer.start()
    print(f'[Dispatch] Watching {filepath}')
    return observer

def run_auth_flow():
    print('[Dispatch] No Discord token found. Starting auth flow...')

    auth_result = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)

            if 'token' in params:
                auth_result['token'] = params['token'][0]
                auth_result['discord_id'] = params['discord_id'][0]
                auth_result['discord_username'] = params['discord_username'][0]

                self.send_response(200)
                self.send_header('Content-type', 'text/html')
                self.end_headers()
                self.wfile.write(b'''
                    <html><body style="background:#0f1117;color:#fff;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;">
                    <div style="text-align:center;">
                    <h1 style="color:#f5a623;">The Dispatch</h1>
                    <p>Authentication successful. You can close this window.</p>
                    </div></body></html>
                ''')
            else:
                self.send_response(400)
                self.end_headers()

        def log_message(self, format, *args):
            pass

    server = HTTPServer(('localhost', 8080), CallbackHandler)
    webbrowser.open(f'{SERVER_URL}/auth/login?client_callback=http://localhost:8080/callback')

    print('[Dispatch] Waiting for Discord login...')
    server.handle_request()

    if auth_result:
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
        with open(env_path, 'w') as f:
            f.write(f'SERVER_URL={SERVER_URL}\n')
            f.write(f'DISCORD_TOKEN={auth_result["token"]}\n')
            f.write(f'DISCORD_ID={auth_result["discord_id"]}\n')
            f.write(f'DISCORD_USERNAME={auth_result["discord_username"]}\n')
            f.write(f'SAVE_PATH={SAVE_PATH}\n')

        print(f'[Dispatch] Authenticated as {auth_result["discord_username"]}')
        return auth_result
    else:
        print('[Dispatch] Auth failed.')
        return None

def main():
    global DISCORD_TOKEN, DISCORD_ID, DISCORD_USERNAME

    if not DISCORD_TOKEN or DISCORD_TOKEN.strip() == '' or DISCORD_TOKEN == 'your_discord_token_here':
        result = run_auth_flow()
        if not result:
            return
        load_dotenv(override=True)
        DISCORD_TOKEN = os.getenv('DISCORD_TOKEN', '')
        DISCORD_ID = os.getenv('DISCORD_ID', '')
        DISCORD_USERNAME = os.getenv('DISCORD_USERNAME', '')

    filepath = SAVE_PATH or find_save_file()
    if not filepath:
        print('[Dispatch] Could not find ATS save file. Set SAVE_PATH in .env')
        return

    print(f'[Dispatch] Found save at: {filepath}')
    start_telemetry_loop()
    push_data(filepath)

    observer = start_watcher(filepath)

    def on_quit(icon, item):
        observer.stop()
        icon.stop()

    icon = pystray.Icon(
        'The Dispatch',
        create_tray_icon(),
        'The Dispatch',
        menu=pystray.Menu(pystray.MenuItem('Quit', on_quit))
    )

    threading.Thread(target=observer.join, daemon=True).start()
    icon.run()

if __name__ == '__main__':
    main()