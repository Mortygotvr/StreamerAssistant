"""
Unified StreamerAlertRelay
==========================
Combines Twitch, Kick, and YouTube chat/alert monitoring into a single script.
Removes Playwright dependency for YouTube (uses pytchat).
Includes Twitch GQL badge fetching.

Author: GitHub Copilot (refactoring existing work)
Date: 28 Feb 2026

Special Thanks to the Open Source Authors & Maintainers:
- aiohttp: Andrey Svetlov & aio-libs
- ollama: Ollama Team
- Pillow (PIL): Alex Clark & Contributors
- pyaudio: Hubert Pham
- pystray: Moses Palmér
- pytchat: taizan-hokuto
- requests: Kenneth Reitz
- vaderSentiment: C.J. Hutto
- websockets: Aymeric Augustin

--------------------------------------------------------------------------------
[ARCHITECTURAL NOTE - PLEASE READ]
1. PURPOSE:
   - This program acts as a *Relay* between Streaming Platforms (Twitch, Kick, YouTube)
     and Local Tools (SAMMI, Overlays, Local TTS Engine).
   - It is designed to *Monitor* chat events anonymously where possible, unifying chat payloads.

2. CHAT MONITORING & AI MODERATION:
   - **CHAT (READING):** configured via "Zones" in settings.html.
     - These parsers (TwitchParser, KickParser, YouTubeParser) are designed to READ
       chat without requiring a user login. They are "ReadOnly" by default.
     - A user can add multiple zones (e.g. monitor 3 different Twitch channels).
   - **MODERATION (ANALYSIS):** The app acts as an AI analysis engine cascading in 3 tiers:
     a) Regex Fast-Pass Filter (Catches obvious links/IPs to bypass AI overhead)
     b) Vader Sentiment Analysis (Fast, Offline, English-only heuristic base layer)
     c) Ollama Local LLM (Deep context, multi-lingual, slow, catches toxic spam/hate speech)
   - **MODERATION (ACTION):** Offloaded to SAMMI and Overlay. 
     - If the AI flags a message, it sends the trigger natively with action flags.
     - Frontends like the Chat Overlay receive deletion events by UUID to strip bad messages retroactively.
     - Unsafe messages skip local TTS (Text-to-Speech) readout if configured.

3. DATA FLOW:
   - Platform -> [Parser creates uniform payload + UUID] -> [Event Queue] -> [Overlay Broadcast] -> [Regex/Vader/LLM Check] -> [SAMMI Trigger & Overlay Mod Event] -> [TTS Engine Queue]
   - Authenticated Actions are side-effects handled by SAMMI, not the primary data flow.

4. LIFECYCLE & STATE MANAGEMENT:
   - **Soft Reloading:** When web UI configurations naturally save, the application issues a `RELOAD_CONFIG_PENDING` signal. This gracefully cancels asynchronous Python `asyncio.Task` event listener loops, dynamically replaces configuration memory variables (`GLOBAL_CONFIG`), and restarts threads mapping to new channels WITHOUT killing the host `python.exe` process wrapper. This preserves System Tray context hooks and averts critical PyInstaller cache lock violations (`_MEIxxxx`).
   - Local PyInstaller execution paths actively separate underlying resources (`creationflags=subprocess.CREATE_NO_WINDOW`, detached processing) to detach console overhead where achievable.
   - **Dynamic TTS Models:** Piper TTS dynamically downloads HuggingFace voice weights to the local `./piper/` directory upon demand.
--------------------------------------------------------------------------------
"""

import asyncio
import aiohttp
import datetime
import urllib.parse
import json
import logging
import os
import queue
import random
import re
import time
import string
import sys
import threading
import urllib.request
import websockets
import requests
import webbrowser
import keyboard
try:
    import ollama
    HAS_OLLAMA = True
except ImportError:
    HAS_OLLAMA = False
    # print("[WARN] ollama not installed.")
try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    HAS_VADER = True
    VADER_ANALYZER = SentimentIntensityAnalyzer()
except ImportError:
    HAS_VADER = False
    VADER_ANALYZER = None
    # print("[WARN] vaderSentiment not installed.")

# Piper will be downloaded dynamically if missing, so TTS is supported
HAS_TTS = True

try:
    import pystray
    from PIL import Image, ImageDraw
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False

# Removed Legacy Auth Dependencies (cryptography, secretstorage, etc.)

from contextlib import contextmanager

@contextmanager
def suppress_alsa_errors():
    """Context manager to suppress ALSA C-level stderr output."""
    devnull = None
    old_stderr = None
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        old_stderr = os.dup(sys.stderr.fileno())
        os.dup2(devnull, sys.stderr.fileno())
    except Exception:
        pass
    try:
        yield
    finally:
        if old_stderr is not None:
            try:
                os.dup2(old_stderr, sys.stderr.fileno())
                os.close(old_stderr)
            except Exception:
                pass
        if devnull is not None:
            try:
                os.close(devnull)
            except Exception:
                pass

# ==============================================================================
# 0. CONFIGURATION & GLOBALS
# ==============================================================================

def get_base_path():
    """Get absolute path to resource, works for dev and for PyInstaller"""
    try:
        # PyInstaller creates a temp folder and stores path in _MEIPASS
        # But we want the directory where the .exe ACTUALLY lives for config/HTML files
        if getattr(sys, 'frozen', False):
            return os.path.dirname(sys.executable)
        else:
            return os.path.dirname(os.path.abspath(__file__))
    except Exception:
        return os.getcwd()

BASE_DIR = get_base_path()

CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
SAMMI_SETTINGS_FILE = os.path.join(BASE_DIR, "sammi_settings.json")
SUB_CACHE_FILE = os.path.join(BASE_DIR, "sub_cache.json")
BADGE_DB = {}  # Global Twitch Badge cache
SUB_DB = {"Twitch": {}, "Kick": {}, "YouTube": {}} # Global Database of Subscribers

# Control Flags
STOP_EVENT = threading.Event()

# SAMMI Globals
SAMMI_URL = "http://localhost:9450/webhook"
SAMMI_PASS = None

def load_sammi_settings():
    global SAMMI_URL, SAMMI_PASS
    try:
        if os.path.exists(SAMMI_SETTINGS_FILE):
            with open(SAMMI_SETTINGS_FILE, "r") as f:
                data = json.load(f)
                SAMMI_URL = data.get("sammi_url", SAMMI_URL)
                SAMMI_PASS = data.get("sammi_password", None)
            print(f"[SAMMI] Settings loaded. URL: {SAMMI_URL}")
    except Exception as e:
        print(f"[SAMMI] Error loading settings: {e}")

def load_sub_cache():
    global SUB_DB
    try:
        if os.path.exists(SUB_CACHE_FILE):
            with open(SUB_CACHE_FILE, "r") as f:
                data = json.load(f)
                # Ensure backwards capability with flat schemas and load channel dicts
                for plat in ["Twitch", "Kick", "YouTube"]:
                    plat_data = data.get(plat, {})
                    is_flat = any(isinstance(v, bool) for v in plat_data.values()) if plat_data else False
                    if is_flat:
                        SUB_DB[plat] = {"Default_Migrated_Channel": plat_data}
                    else:
                        SUB_DB[plat] = plat_data
            
            # Count records dynamically across all channels in all platforms
            count = sum(sum(len(channel_db) for channel_db in db.values()) for db in SUB_DB.values())
            print(f"[SubCache] Loaded {count} cached sub records.")
    except Exception as e:
        print(f"[SubCache] Error loading sub cache: {e}")

def save_sub_cache():
    try:
        with open(SUB_CACHE_FILE, "w") as f:
            json.dump(SUB_DB, f, indent=4)
    except Exception as e:
        print(f"[SubCache] Error saving cache: {e}")

def send_to_sammi(payload):
    """
    Sends a JSON payload to the SAMMI webhook.
    """
    if not isinstance(payload, dict): return
    headers = {"Content-Type": "application/json"}
    if SAMMI_PASS: headers["Authorization"] = SAMMI_PASS
    
    # Run in thread to not block main loop
    def _send():
        try:
            resp = requests.post(SAMMI_URL, json=payload, headers=headers, timeout=2)
            if resp.status_code != 200:
                print(f"[SAMMI] Failed: {resp.status_code} {resp.text}")
            else:
                # Optionally print success, but clearly mark it as a triggered event, not a "test"
                # To prevent console spam for standard chat messages, we can print it optionally.
                # Since the user requested the output, I'll log it clearly:
                print(f"[SAMMI] Sent event: {payload.get('trigger')} | Status: {resp.status_code}")
        except Exception as e:
            print(f"[SAMMI] Error sending: {e}") 
            
    threading.Thread(target=_send, daemon=True).start()

# Global Event Queue for all drivers
# Items: (parser_name, channel/user, event_key, trigger_text, custom_data_dict)
event_queue = queue.Queue()

# Global config state mutable by tray
GLOBAL_CONFIG = {}
TRAY_ICON = None
RESTART_PENDING = False
RELOAD_CONFIG_PENDING = False

# --- TTS WORKER ---
tts_queue = queue.Queue()

def _play_audio_stream(file_path, device_name=None, volume=1.0):
    """
    Helper function to play an audio file natively via ffplay (or afplay on macOS).
    """
    import os
    import sys
    import subprocess

    if not os.path.exists(file_path):
        return
        
    vol_val = int(volume * 100)

    if sys.platform == "win32":
        ffplay_path = os.path.join(BASE_DIR, "ffplay.exe")
        cmd = [ffplay_path, "-nodisp", "-autoexit"]
        if vol_val != 100:
            cmd.extend(["-volume", str(vol_val)])
        cmd.append(file_path)
        
        env = os.environ.copy()
        if device_name and device_name.lower() != "system default":
            env["SDL_AUDIO_DEVICE_NAME"] = device_name
            env["SDL_AUDIODRIVER"] = "" # Explicitly clear any existing driver to let SDL auto-detect

        try:
            result = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception as e:
            print(f"[TTS] ffplay Error: {e}")

    elif sys.platform == "darwin":
        subprocess.run(["afplay", "-v", str(volume), file_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def _generate_bell_wav(bell_path, output_path):
    import math
    import struct
    import wave
    
    # Generate "ding" if no custom file is provided
    if not (bell_path and os.path.isfile(bell_path)):
        duration = 0.2
        f = 800.0
        sample_rate = 44100
        num_samples = int(sample_rate * duration)
        samples = (math.sin(2 * math.pi * k * f / sample_rate) for k in range(num_samples))
        # Base volume is 1.0 
        data = b''.join(struct.pack('<h', int(sample * 1.0 * (1.0 - k/num_samples) * 32767)) 
                      for k, sample in enumerate(samples))
        with wave.open(output_path, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(data)
        return True
    return False

def tts_worker():
    if not HAS_TTS: return

    import subprocess
    import tempfile

    while not STOP_EVENT.is_set():
        try:
            item = tts_queue.get(timeout=1)
            if item is None: break
            
            # Handle both old (4 items) and new (5 items) queue formats
            if len(item) == 4:
                text, volume, rate, voice_id = item
                device_name = None
            else:
                text, volume, rate, voice_id, device_name = item
                
            try:
                # ==========================================
                # 1. BELL LOGIC
                # ==========================================
                if text == "[BELL]":
                    bell_path = str(voice_id).strip() if voice_id else ""
                    
                    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as gen_tmp:
                        gen_file = gen_tmp.name
                        
                    is_generated = _generate_bell_wav(bell_path, gen_file)
                    source_file = gen_file if is_generated else bell_path
                    
                    # Play Bell
                    try:
                        _play_audio_stream(source_file, device_name, volume=volume)
                    except Exception as e:
                        print(f"[TTS] Bell Playback Error: {e}")
                    finally:
                        for f in [gen_file]:
                            if f and os.path.exists(f):
                                try: os.remove(f)
                                except: pass
                    continue

                # ==========================================
                # 2. TTS LOGIC (Piper)
                # ==========================================
                if not voice_id:
                    # Default piper model
                    voice_id = "en_US-lessac-low.onnx"

                model_base_path = os.path.join(BASE_DIR, "piper")

                # Check if it's just the ONNX file name, else construct the full path
                if not voice_id.endswith(".onnx"):
                    voice_id += ".onnx"
                    
                model_path = os.path.join(model_base_path, voice_id)
                if not os.path.exists(model_path):
                    # fallback to default
                    model_path = os.path.join(model_base_path, "en_US-lessac-low.onnx")

                piper_exe = os.path.join(model_base_path, "piper.exe")

                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                    temp_filename = tmp.name
                    
                import subprocess
                try:
                    process = subprocess.Popen(
                        [piper_exe, "-m", model_path, "-f", temp_filename],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE
                    )
                    stdout, stderr = process.communicate(input=text.encode('utf-8'))
                except Exception as e:
                    print(f"[TTS] Piper Error: {e}")
                
                try:
                    _play_audio_stream(temp_filename, device_name, volume=volume)
                except Exception as e:
                    print(f"[TTS] Audio Playback Error: {e}")
                finally:
                    if os.path.exists(temp_filename):
                        try: os.remove(temp_filename)
                        except: pass
                        
            except Exception as e:
                print(f"[TTS] Worker Error: {e}")
                    
            tts_queue.task_done()
        except queue.Empty:
            continue
                    
            tts_queue.task_done()
        except queue.Empty:
            continue

if HAS_TTS:
    threading.Thread(target=tts_worker, daemon=True).start()

# --- HELPER FUNCTIONS ---
def log_debug_data(source, data):
    """
    Optional: log raw payloads (commented out by default to save disk).
    """
    # with open("debug_log.txt", "a", encoding="utf-8") as f:
    #     f.write(f"[{source}] {data}\n")
    pass

# ==============================================================================
# 1. WEBSOCKET SERVER (Overlay Communication)
# ==============================================================================

CLIENTS = set()
_ws_loop = None

def _get_audio_devices():
    devices = []
    import sys
    if sys.platform == "win32":
        try:
            import subprocess
            # Use PowerShell to get full WASAPI endpoint names instead of WinMM to avoid 31-character limit
            cmd = ['powershell', '-NoProfile', '-Command', 
                   "$OutputEncoding = [System.Text.Encoding]::UTF8; [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Get-PnpDevice -Class AudioEndpoint | Where-Object { $_.InstanceId -match '0\\.0\\.0\\.00000000' } | Select-Object -ExpandProperty FriendlyName"]
            out = subprocess.check_output(cmd, creationflags=subprocess.CREATE_NO_WINDOW, stderr=subprocess.DEVNULL)
            dec = out.decode('utf-8', errors='ignore')
            for line in dec.split('\n'):
                name = line.strip()
                if name and name not in devices:
                    devices.append(name)
        except Exception as e:
            print(f"[Device List] Error getting devices: {e}")
    return devices

async def get_tts_info():
    import platform
    import shutil
    info = {"voices": [], "devices": [], "os": platform.system()}
    
    # Check for FFplay
    ffplay_path = os.path.join(BASE_DIR, "ffplay.exe") if platform.system() == "Windows" else "ffplay"
    info["has_ffplay"] = os.path.exists(ffplay_path) or shutil.which("ffplay") is not None
    
    # Check for Piper
    piper_path = os.path.join(BASE_DIR, "piper", "piper.exe") if platform.system() == "Windows" else os.path.join(BASE_DIR, "piper", "piper")
    info["has_piper"] = os.path.exists(piper_path) or shutil.which("piper") is not None
    
    if HAS_TTS:
        # Default piper voice
        info["voices"].append({"id": "en_US-lessac-low.onnx", "name": "en_US-lessac-low (en-US)"})
        # Users can add more Piper models in the 'piper' folder
        try:
            piper_dir = os.path.join(BASE_DIR, "piper")
            if os.path.exists(piper_dir):
                for f in os.listdir(piper_dir):
                    if f.endswith(".onnx") and f != "en_US-lessac-low.onnx":
                        info["voices"].append({"id": f, "name": f.replace(".onnx", " (Piper)")})
        except Exception as e:
            print(f"[TTS] Error getting voices: {e}")
            
    for d in _get_audio_devices():
        info["devices"].append({"id": d, "name": d})
    return info



async def _install_piper_task(websocket):
    import urllib.request
    import zipfile
    import os
    try:
        PIPER_ZIP_URL = "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_windows_amd64.zip"
        zip_path = os.path.join(BASE_DIR, "piper_temp.zip")
        MODEL_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/low/en_US-lessac-low.onnx"
        JSON_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/low/en_US-lessac-low.onnx.json"

        await websocket.send(json.dumps({"type": "status", "message": "Downloading Piper TTS..."}))
        await asyncio.to_thread(urllib.request.urlretrieve, PIPER_ZIP_URL, zip_path)

        await websocket.send(json.dumps({"type": "status", "message": "Extracting Piper..."}))
        def _extract_and_download():
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(BASE_DIR)
            if os.path.exists(zip_path):
                os.remove(zip_path)
            
            piper_dir = os.path.join(BASE_DIR, "piper")
            os.makedirs(piper_dir, exist_ok=True)
            model_path = os.path.join(piper_dir, "en_US-lessac-low.onnx")
            json_path = os.path.join(piper_dir, "en_US-lessac-low.onnx.json")
            
            if not os.path.exists(model_path):
                urllib.request.urlretrieve(MODEL_URL, model_path)
            if not os.path.exists(json_path):
                urllib.request.urlretrieve(JSON_URL, json_path)

        await websocket.send(json.dumps({"type": "status", "message": "Downloading default voice..."}))
        await asyncio.to_thread(_extract_and_download)

        await websocket.send(json.dumps({"type": "status", "message": "Piper TTS Installed Successfully!"}))
        # Notifying to show as installed
        await websocket.send(json.dumps({"type": "piper_installed"}))
    except Exception as e:
        await websocket.send(json.dumps({"type": "status", "message": f"Error installing Piper: {e}"}))

async def _get_piper_voices_list(websocket):
    import urllib.request
    import json
    try:
        url = "https://huggingface.co/rhasspy/piper-voices/raw/main/voices.json"
        
        def _fetch():
            req = urllib.request.urlopen(url, timeout=10)
            return req.read().decode('utf-8')
            
        data = await asyncio.to_thread(_fetch)
        voices_dict = json.loads(data)
        
        # Parse it nicely
        voices_out = []
        for key, info in voices_dict.items():
            lang = info.get("language", {}).get("name_english", "Unknown")
            name = info.get("name", key)
            quality = info.get("quality", "")
            voices_out.append({
                "key": key,
                "display": f"{name} ({lang} - {quality})",
                "files": info.get("files", {})
            })
            
        voices_out.sort(key=lambda x: (x["display"]))
        
        await websocket.send(json.dumps({
            "type": "piper_voices_list_result",
            "payload": voices_out
        }))
    except Exception as e:
        await websocket.send(json.dumps({"type": "status", "message": f"Failed to fetch voices list: {e}"}))

async def _install_piper_voice_task(websocket, voice_key, files_dict):
    import urllib.request
    import os
    try:
        piper_dir = os.path.join(BASE_DIR, "piper")
        os.makedirs(piper_dir, exist_ok=True)
        base_url = "https://huggingface.co/rhasspy/piper-voices/resolve/main/"

        await websocket.send(json.dumps({"type": "status", "message": f"Downloading voice {voice_key}..."}))

        def _download():
            for f in files_dict.keys():
                if f.endswith(".onnx") or f.endswith(".json"):
                    filename = os.path.basename(f)
                    out_path = os.path.join(piper_dir, filename)
                    urllib.request.urlretrieve(base_url + f, out_path)
        await asyncio.to_thread(_download)
        
        await websocket.send(json.dumps({"type": "status", "message": f"Voice {voice_key} downloaded!"}))
        await websocket.send(json.dumps({"type": "voice_installed", "message": "Voice installed successfully"}))

        # Refresh local TTS info
        tts_info = await get_tts_info()
        await websocket.send(json.dumps({"type": "tts_info", "payload": tts_info}))
    except Exception as e:
        await websocket.send(json.dumps({"type": "status", "message": f"Error downloading voice: {e}"}))

async def _cleanup_piper_voices(websocket, keep_voice_id):
    import os
    try:
        piper_dir = os.path.join(BASE_DIR, "piper")
        if not os.path.exists(piper_dir):
            return

        # The key we want to keep might just be "en_US-lessac-low.onnx"
        keep_base = keep_voice_id.replace(".onnx", "")
        deleted_count = 0

        for f in os.listdir(piper_dir):
            if f.endswith(".onnx") or f.endswith(".json"):
                # Always keep the required lessac low as fallback? Optional.
                if not f.startswith(keep_base):
                    os.remove(os.path.join(piper_dir, f))
        
        # Refresh local TTS info
        tts_info = await get_tts_info()
        await websocket.send(json.dumps({"type": "tts_info", "payload": tts_info}))
    except Exception as e:
        await websocket.send(json.dumps({"type": "status", "message": f"Error cleaning voices: {e}"}))

async def _install_ffplay_task(websocket):
    import urllib.request
    import zipfile
    import shutil
    try:
        url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
        zip_path = os.path.join(BASE_DIR, "ffmpeg_temp.zip")

        await websocket.send(json.dumps({"type": "status", "message": "Downloading FFplay (this may take a minute)..."}))

        # Download
        await asyncio.to_thread(urllib.request.urlretrieve, url, zip_path)

        await websocket.send(json.dumps({"type": "status", "message": "Extracting FFplay..."}))

        # Extract
        def _extract():
            with zipfile.ZipFile(zip_path, 'r') as z:
                for fname in z.namelist():
                    if fname.endswith("ffplay.exe"):
                        ffplay_out = os.path.join(BASE_DIR, "ffplay.exe")
                        with z.open(fname) as source, open(ffplay_out, "wb") as target:
                            shutil.copyfileobj(source, target)
                        break

        await asyncio.to_thread(_extract)

        if os.path.exists(zip_path):
            os.remove(zip_path)

        await websocket.send(json.dumps({"type": "status", "message": "FFplay Installed Successfully!"}))
        await websocket.send(json.dumps({"type": "ffplay_installed"}))
    except Exception as e:
        print(f"[Install] FFplay install error: {e}")
        await websocket.send(json.dumps({"type": "status", "message": f"Error installing FFplay: {e}"}))

async def ws_register(websocket):
    global _ws_loop
    if _ws_loop is None:
        try:
            _ws_loop = asyncio.get_running_loop()
        except AttributeError:
             _ws_loop = asyncio.get_event_loop()

    CLIENTS.add(websocket)
    try:
        # Send current config on connect so the settings page is pre-filled
        try:
            current = load_complete_config_state()
            await websocket.send(json.dumps({"type": "config_state", "payload": current}))
            
            # Send TTS info
            tts_info = await get_tts_info()
            await websocket.send(json.dumps({"type": "tts_info", "payload": tts_info}))
        except Exception as e:
            print(f"[WS] Error sending initial config: {e}")

        async for message in websocket:
            try:
                data = json.loads(message)
                if data.get("type") == "install_ffplay":
                    asyncio.create_task(_install_ffplay_task(websocket))
                elif data.get("type") == "install_piper":
                    asyncio.create_task(_install_piper_task(websocket))
                elif data.get("type") == "get_piper_voices":
                    asyncio.create_task(_get_piper_voices_list(websocket))
                elif data.get("type") == "install_piper_voice":
                    asyncio.create_task(_install_piper_voice_task(websocket, data.get("voice_key"), data.get("files")))
                elif data.get("type") == "cleanup_piper_voices":
                    asyncio.create_task(_cleanup_piper_voices(websocket, data.get("keep_voice_id")))
                elif data.get("type") == "save_config":

                    new_cfg = data.get("payload")
                    if new_cfg:
                        # PRESERVE TRAY-MANAGED SETTINGS (Volume & Device ID)
                        if "tts" in GLOBAL_CONFIG and "tts" in new_cfg:
                            if "device_id" in GLOBAL_CONFIG["tts"]:
                                new_cfg["tts"]["device_id"] = GLOBAL_CONFIG["tts"]["device_id"]
                            elif "device_id" not in new_cfg["tts"]:
                                new_cfg["tts"]["device_id"] = ""

                            if "volume" in GLOBAL_CONFIG["tts"]:
                                new_cfg["tts"]["volume"] = GLOBAL_CONFIG["tts"]["volume"]
                            elif "volume" not in new_cfg["tts"]:
                                new_cfg["tts"]["volume"] = 1.0

                        save_complete_config_state(new_cfg)
                        
                        # Notify client
                        await websocket.send(json.dumps({"type": "status", "message": "Configuration Saved! Reloading Listeners..."}))
                        print("[CONFIG] New configuration saved via WebSocket. Reloading Listeners...")

                        # Give time for the message to send
                        await asyncio.sleep(1)

                        global RELOAD_CONFIG_PENDING
                        RELOAD_CONFIG_PENDING = True

                elif data.get("type") == "test_sammi_trigger":
                    trigger_val = data.get("trigger", "Test Trigger")
                    user_val = data.get("user", "TestUser")
                    channel_val = data.get("channel", "TestChannel")
                    var_val = data.get("var", "500")
                    if trigger_val:
                        print(f"[WS] Sending test SAMMI trigger: {trigger_val}")
                        # Extract platform from the beginning if possible to mimic real payloads
                        platform_val = "Test"
                        if trigger_val.lower().startswith("twitch "): platform_val = "Twitch"
                        elif trigger_val.lower().startswith("kick "): platform_val = "Kick"
                        elif trigger_val.lower().startswith("youtube "): platform_val = "YouTube"

                        # Inject "testserver" into the trigger for proper SAMMI and downstream parsing if requested
                        # Splitting out the platform and adding "testserver " if it doesn't already exist
                        if platform_val != "Test" and "testserver" not in trigger_val.lower():
                            trigger_suffix = trigger_val[len(platform_val):].strip()
                            trigger_val = f"{platform_val} testserver {trigger_suffix}"
                        elif "testserver" not in trigger_val.lower():
                            trigger_val = f"testserver {trigger_val}"

                        # Dynamic Subscriber Caching Check for Test Payloads
                        test_sub_status = "unknown"
                        chat_cache_enabled = False
                        for c_id, c_data in GLOBAL_CONFIG.get("chats", {}).items():
                            if c_data.get("input", "").lower() == channel_val.lower():
                                chat_cache_enabled = c_data.get("sub_cache_enabled", False)
                                break

                        if chat_cache_enabled and platform_val in SUB_DB:
                            # Use provided test channel name natively, else fall back to scanning all for a best guess
                            test_channel_db = SUB_DB[platform_val].get(channel_val.lower())
                            if not test_channel_db:
                                for chn_db in SUB_DB[platform_val].values():
                                    if user_val.lower() in chn_db:
                                        test_channel_db = chn_db
                                        break
                                if not test_channel_db: test_channel_db = {}
                                
                            cached_status = test_channel_db.get(user_val.lower())
                            if cached_status is not None:
                                test_sub_status = bool(cached_status)
                        
                        # Mock event that explicitly asserts true if it's a test sub
                        if "sub" in trigger_val.lower() or "member" in trigger_val.lower():
                            test_sub_status = True

                        payload = {
                            "trigger": trigger_val,
                            "msg_id": f"test-uuid-{int(time.time())}",
                            "platform": platform_val,
                            "username": user_val,
                            "message": "",
                            "customData": {
                                "is_test": True,
                                "username": user_val,
                                "display_name": user_val,
                                "user": user_val,  # For compatibility
                                "user_id": "test-user-id-12345",
                                "room_id": "test-room-id",
                                "subscriber": test_sub_status,
                                "mod": False,
                                "vip": False,
                                "badges_html": "<img src='https://static-cdn.jtvnw.net/badges/v1/5d9f2208-5dd8-11e7-8513-2ff4adfae661/1' alt='sub'/>",
                                "color": "#FF5733"
                            }
                        }

                        t_lower = trigger_val.lower()
                        if "chat" in t_lower:
                            payload["message"] = f"This is a test message. Variable used: {var_val}"
                            payload["customData"]["message"] = payload["message"]
                        elif "redeem" in t_lower:
                            payload["customData"]["reward_id"] = "test-reward-id-9999"
                            payload["customData"]["reward_title"] = var_val if not var_val.isdigit() else "Test Reward"
                            payload["customData"]["cost"] = int(var_val) if var_val.isdigit() else 500
                            payload["customData"]["image"] = "https://static-cdn.jtvnw.net/custom-reward-images/default-4.png"
                        elif "raid" in t_lower:
                            payload["customData"]["viewers"] = int(var_val) if var_val.isdigit() else 10
                            payload["customData"]["raider"] = user_val
                        elif "sub" in t_lower or "member" in t_lower:
                            payload["customData"]["type"] = "sub"
                            payload["customData"]["tier"] = var_val if var_val.isdigit() else "1000"
                        elif "cheer" in t_lower or "super" in t_lower:
                            payload["customData"]["bits"] = int(var_val) if var_val.isdigit() else 100
                            payload["customData"]["amount"] = payload["customData"]["bits"]
                            payload["message"] = f"This is a test cheer! Variable: {var_val}"
                            payload["customData"]["message"] = payload["message"]
                            
                        # Fire event to SAMMI Bridge
                        send_to_sammi(payload)
                        
                        # Fire EVENT structure mimicking main loop for overlays
                        broadcast({
                            "type": "moderation",
                            "payload": payload
                        })
                        
                        if "chat" in trigger_val.lower():
                            broadcast({
                                "type": "chat",
                                "trigger": trigger_val, # [ARCHITECTURAL NOTE] Trigger passed for websocket persistence explicitly
                                "msg_id": payload["msg_id"],
                                "source": f"{platform_val} testserver",
                                "channel": channel_val,
                                "platform": platform_val.lower(),
                                "username": user_val,
                                "message": payload["message"],
                                "badges": payload["customData"]["badges_html"],
                                "color": "#FF5733",
                                "avatar": "https://static-cdn.jtvnw.net/emoticons/v2/emotesv2_b5c8be0804b94f0fb453cdba307fa924/default/dark/1.0"
                            })
                        else:
                            broadcast({
                                "type": "alert",
                                "source": f"{platform_val} testserver",
                                "event": trigger_val,
                                "trigger": trigger_val, # [ARCHITECTURAL NOTE] Trigger passed for websocket persistence explicitly
                                "data": payload["customData"]
                            })

                        # Process hotkeys locally for the test trigger as well
                        hotkeys = GLOBAL_CONFIG.get("hotkeys", [])
                        for hk in hotkeys:
                            hk_trigger = hk.get("trigger", "").strip()
                            if not hk_trigger: continue
                            pattern_str = "^" + re.escape(hk_trigger).replace(r"\*", ".*") + "$"
                            try:
                                if re.match(pattern_str, trigger_val, re.IGNORECASE):
                                    modifiers = []
                                    if hk.get("ctrl"): modifiers.append("ctrl")
                                    if hk.get("alt"): modifiers.append("alt")
                                    if hk.get("shift"): modifiers.append("shift")
                                    key = hk.get("key", "").lower()
                                    if key:
                                        modifiers.append(key)
                                        hotkey_str = "+".join(modifiers)
                                        print(f"[Hotkeys] TEST Trigger '{trigger_val}' matched! Simulating keystroke: {hotkey_str}")
                                        try:
                                            import keyboard
                                            keys_to_press = hotkey_str.split('+')
                                            for k in keys_to_press: keyboard.press(k)
                                            await asyncio.sleep(0.1) # Hold for 100ms so SAMMI can catch it
                                        except Exception as hk_e:
                                            print(f"Failed to send hotkey {hotkey_str}: {hk_e}")
                                        finally:
                                            if 'keyboard' in locals() and 'keys_to_press' in locals():
                                                for k in reversed(keys_to_press):
                                                    try: keyboard.release(k)
                                                    except: pass
                            except Exception as e:
                                print(f"[Hotkeys] Regex error parsing trigger '{hk_trigger}': {e}")

            except Exception as e:
                print(f"[WS] Message Error: {e}")
    except Exception:
        pass
    finally:
        CLIENTS.remove(websocket)

def load_complete_config_state():
    """
    Reads both the unified config structure or legacy separated files.
    Returns a dict: { "sammi": {...}, "chats": {...} }
    """
    state = {
        "sammi": {"sammi_url": "http://localhost:9450/webhook", "sammi_password": ""},
        "chats": {},
        "moderation": {
            "vader_enabled": False,
            "vader_threshold": -0.5,
            "ollama_enabled": False,
            "ollama_url": "http://localhost:11434",
            "ollama_model": "llama3",
            "ollama_prompt": "Analyze the following chat message. If it contains hate speech, severe toxicity, or spam, reply with \"FLAGGED\". Otherwise, reply with \"CLEAN\"."
        },
        "tts": {"enabled": False, "rate": 200, "ignore_commands": True, "read_username": False, "voice_id": ""}
    }
    
    # Try reading unified config first (if we migrated)
    # We will repurpose config.json to hold everything
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
                state["sammi"] = data.get("sammi", state["sammi"])
                state["moderation"] = data.get("moderation", state["moderation"])
                state["tts"] = data.get("tts", state["tts"]) # Load TTS config
                state["hotkeys"] = data.get("hotkeys", []) # Load Hotkeys
                state["subscriber_cache"] = data.get("subscriber_cache", False) # Load toggle
                state["st_filters"] = data.get("st_filters", {}) # Load ST filters

                # Load Platform Configs
                state["twitch"] = data.get("twitch", {})
                state["youtube"] = data.get("youtube", {})
                state["kick"] = data.get("kick", {})
                
                # Migration: 'zones' -> 'chats'
                if "chats" in data:
                    state["chats"] = data.get("chats", {})
                elif "zones" in data:
                    state["chats"] = data.get("zones", {})
                elif data:
                    # Fallback old simple structure
                    state["chats"] = {k: v for k,v in data.items() if k.startswith("zone") or k.startswith("chat")}
                    
                return state
        except Exception:
            pass

    # If we are here, we might have sammi_settings.json separately
    if os.path.exists(SAMMI_SETTINGS_FILE):
        try:
            with open(SAMMI_SETTINGS_FILE, "r") as f:
                s_data = json.load(f)
                state["sammi"].update(s_data)
        except Exception:
            pass
            
    return state

def save_complete_config_state(new_state):
    """
    Saves the dictionary as a single unified config.json
    """
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(new_state, f, indent=4)
        print(f"[CONFIG] Saved unified config to {CONFIG_FILE}")
        
        # Update globals immediately where possible
        global SAMMI_URL, SAMMI_PASS
        sammi = new_state.get("sammi", {})
        SAMMI_URL = sammi.get("sammi_url", SAMMI_URL)
        SAMMI_PASS = sammi.get("sammi_password", SAMMI_PASS)
        
    except Exception as e:
        print(f"[CONFIG] Error saving config: {e}")

async def _broadcast_internal(message_dict):
    if not CLIENTS:
        return
    payload = json.dumps(message_dict)
    dead_clients = set()
    for ws in list(CLIENTS):
        try:
            await ws.send(payload)
        except Exception:
            dead_clients.add(ws)
    for ws in dead_clients:
        CLIENTS.discard(ws)

async def ws_main_server(host, port):
    async with websockets.serve(ws_register, host, port):
        await asyncio.Future()  # Run forever

def start_ws_server(host="0.0.0.0", port=41837):
    def run():
        global _ws_loop
        _ws_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_ws_loop)
        print(f"[WS] Starting WebSocket Server on ws://{host}:{port}")
        try:
            _ws_loop.run_until_complete(ws_main_server(host, port))
        except Exception as e:
            print(f"[WS] Server Error: {e}")
        finally:
            _ws_loop.close()

    t = threading.Thread(target=run, daemon=True, name="WebSocketServer")
    t.start()

def broadcast(data):
    global _ws_loop
    # Debug print to confirm broadcast attempt
    # if data.get("type") == "chat":
    #    print(f"[WS DEBUG] JSON: {json.dumps(data)}")
    
    if _ws_loop and _ws_loop.is_running():
        asyncio.run_coroutine_threadsafe(_broadcast_internal(data), _ws_loop)

# ==============================================================================
# 2. TWITCH LOGIC
# ==============================================================================

class TwitchParser:
    INPUT_TYPE = "username"
    VALID_TRIGGERS = {
        'Twitch chat': ['chat', 'message', 'privmsg'],
        'Twitch cheer': ['cheer', 'bits'],
        'Twitch subscription': ['subscription', 'sub', 'resub', 'subgift'],
        'Twitch raid': ['raid'],
        'Twitch follow': ['follow'],
        'Twitch redeem': ['reward-redeemed', 'redemption'],
        'Twitch ad break': ['ad-break', 'commercial'],
        'Twitch hype train': ['hype-train-begin', 'hype-train-progress', 'hype-train-end'],
        'Twitch poll': ['poll-begin', 'poll-progress', 'poll-end'],
        'Twitch prediction': ['prediction-begin', 'prediction-progress', 'prediction-end', 'prediction-lock']
    }

    EVENTS = [
        "Twitch chat", "Twitch cheer", "Twitch redeem (irc)", "Twitch redeem (pubsub)",
        "Twitch sub", "Twitch raid", "Twitch ban", "Twitch timeout",
        "Twitch message delete", "Twitch notice", "Twitch roomstate", "Twitch hype train", "Twitch other"
    ]
    TRIGGERS = {
        "Twitch chat":            "Twitch chat",
        "Twitch cheer":           "Twitch cheer {bits} bits",
        "Twitch redeem (irc)":    "Twitch redeem {short_id}",
        "Twitch redeem (pubsub)": "Twitch redeem {title}",
        "Twitch sub":             "Twitch sub",
        "Twitch raid":            "Twitch raid",
        "Twitch ban":             "Twitch ban",
        "Twitch timeout":         "Twitch timeout",
        "Twitch message delete":  "Twitch message delete",
        "Twitch notice":          "Twitch notice",
        "Twitch roomstate":       "Twitch roomstate",
        "Twitch hype train":      "Twitch hype train",
        "Twitch other":           "Twitch other ({command})"
    }

    @staticmethod
    def get_chat_url(channel):
        return f"https://www.twitch.tv/popout/{channel}/chat?popout="


    @staticmethod
    def parse_frame(line):
         # 1. Handle Notifications (PubSub/Hermes) - usually JSON
        if line.strip().startswith("{"):
            try:
                data = json.loads(line)
                
                # Optimized implementation inspired by original parser structure
                # but heavily adapted for recursive search
                def find_event(obj, target_type):
                        # 1. Dictionary Handling
                        if isinstance(obj, dict):
                            # Try precise match first (EventSub style)
                            if obj.get("type") == target_type:
                                return obj.get("data") or obj
                            
                            # Original parser structure match (Notification -> PubSub)
                            if "notification" in obj:
                                notif = obj["notification"]
                                if isinstance(notif, dict) and "pubsub" in notif:
                                    # Recursion helps if "pubsub" value is a string or dict
                                    return find_event(notif["pubsub"], target_type)

                            # Nested JSON string check
                            if "message" in obj and isinstance(obj["message"], str):
                                try:
                                    inner = json.loads(obj["message"])
                                    res = find_event(inner, target_type)
                                    if res: return res
                                except: pass
                                
                            # General recursion
                            for k, v in obj.items():
                                res = find_event(v, target_type)
                                if res: return res
                                
                        # 2. List Handling
                        elif isinstance(obj, list):
                            for item in obj:
                                res = find_event(item, target_type)
                                if res: return res
                        
                        # 3. String Parser (PubSub often sends JSON as string)
                        elif isinstance(obj, str):
                            s = obj.strip()
                            if s.startswith("{") and s.endswith("}"):
                                try:
                                    inner = json.loads(s)
                                    # If string parsed successfully, recurse into it
                                    res = find_event(inner, target_type)
                                    if res: return res
                                except: pass
                        
                        return None

                # Check for Redemptions
                redemption_data = find_event(data, "reward-redeemed")
                if redemption_data:
                    redemption = redemption_data.get("redemption") or redemption_data
                    user_obj = redemption.get("user") or {}
                    cursor = user_obj.get("id", "0")
                    
                    rew = redemption.get("reward") or {}
                    
                    # Safe image extraction
                    img_obj = rew.get("image") or {}
                    def_img_obj = rew.get("default_image") or rew.get("defaultImage") or {}
                    final_image = img_obj.get("url_4x") or def_img_obj.get("url_4x")

                    return "Twitch redeem (pubsub)", {
                        "trigger": f"Twitch redeem {rew.get('title', 'Unknown')}",
                        "customData": {
                            "reward_id": rew.get("id"),
                            "reward_title": rew.get("title", "Unknown"),
                            "cost": rew.get("cost"),
                            "user_id": user_obj.get("id"),
                            "username": user_obj.get("login"),
                            "display_name": user_obj.get("display_name") or user_obj.get("displayName"),
                            "message": redemption.get("user_input") or redemption.get("userInput", ""),
                            "image": final_image
                        }
                    }
                
                # Check for Hype Train
                hype_train_data = find_event(data, "hype-train-progression")
                if not hype_train_data:
                    hype_train_data = find_event(data, "hype-train-start")
                if not hype_train_data:
                    hype_train_data = find_event(data, "hype-train-end")
                
                if hype_train_data:
                    # Build a list of possible sources for the data
                    sources = [hype_train_data, data]
                    if isinstance(hype_train_data.get("progress"), dict):
                        sources.append(hype_train_data["progress"])
                        if isinstance(hype_train_data["progress"].get("level"), dict):
                            sources.append(hype_train_data["progress"]["level"])
                    if isinstance(hype_train_data.get("level"), dict):
                        sources.append(hype_train_data["level"])

                    # Robust extraction for level
                    level = 0
                    for source in sources:
                        if isinstance(source, dict) and "level" in source:
                            lvl_data = source["level"]
                            if isinstance(lvl_data, dict):
                                level = lvl_data.get("value", 0)
                            elif isinstance(lvl_data, int):
                                level = lvl_data
                            if level:
                                break
                    
                    # Robust extraction for progress
                    progress = 0
                    for source in sources:
                        if isinstance(source, dict) and "progress" in source:
                            prog_data = source["progress"]
                            if isinstance(prog_data, dict):
                                progress = prog_data.get("value", 0)
                            elif isinstance(prog_data, int):
                                progress = prog_data
                            if progress:
                                break
                                
                    # Robust extraction for goal
                    goal = 0
                    for source in sources:
                        if isinstance(source, dict) and "goal" in source:
                            goal_data = source["goal"]
                            if isinstance(goal_data, dict):
                                goal = goal_data.get("value", 0)
                            elif isinstance(goal_data, int):
                                goal = goal_data
                            if goal:
                                break
                                
                    total = hype_train_data.get("total", 0)
                    
                    return "Twitch hype train", {
                        "trigger": f"Twitch hype train {level}",
                        "customData": {
                            "level": level,
                            "progress": progress,
                            "goal": goal,
                            "total": total
                        }
                    }
                
                # Generic catch-all for any valid JSON from PubSub - COMMENTED OUT TO REDUCE NOISE
                # return "Twitch other", {
                #     "trigger": "Twitch pubsub (other)",
                #     "customData": data
                # }
                return None
            except Exception as e:
                # print(f"[TwitchParser] JSON Parse Error: {e}")
                pass

        # 2. Handle IRC Lines
        def parse_irc_tags(tag_str):
            tags = {}
            if not tag_str: return tags
            for part in tag_str.split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    v = v.replace(r"\:", ";").replace(r"\s", " ").replace(r"\\", "\\").replace(r"\r", "\r").replace(r"\n", "\n")
                    tags[k] = v
                else:
                    tags[part] = True
            return tags

        rest = line.strip()
        if not rest or rest.startswith("PING"):
            return None

        tags = {}
        prefix = None
        
        if rest.startswith("@"):
            i = rest.find(" ")
            tags = parse_irc_tags(rest[1:i])
            rest = rest[i+1:].lstrip()

        if rest.startswith(":"):
            i = rest.find(" ")
            prefix = rest[1:i]
            rest = rest[i+1:].lstrip()

        # Helper to get best name
        def get_best_name(tags_dict, prefix_str):
            dn = tags_dict.get("display-name")
            if dn: return dn
            if prefix_str:
                return prefix_str.split("!")[0]
            return "Unknown"

        ti = rest.find(" :")
        text = None
        if ti != -1:
            text = rest[ti+2:]
            rest = rest[:ti].strip()
        
        # print(f"[Parser Debug] Command candidates: {rest} | Text: {text}")
        
        parts = rest.split()
        if not parts: return None
        command = parts[0]

        # Identify shared chat events (messages/alerts from other channels in the collab)
        room_id = tags.get("room-id")
        source_room_id = tags.get("source-room-id")
        is_shared_chat = False
        if room_id and source_room_id and room_id != source_room_id:
            is_shared_chat = True
        
        # We store these to add to the event payloads
        base_custom_data = {
            "room_id": room_id,
            "source_room_id": source_room_id,
            "is_shared_chat": is_shared_chat,
            "user_id": tags.get("user-id")
        }

        # Logic for PRIVMSG
        if command == "PRIVMSG":
            channel = parts[1]
            # print(f"[Parser Debug] Processing PRIVMSG. Raw Badges: {tags.get('badges')}")
            raw_badges = tags.get("badges", "")
            final_badges = process_twitch_badges(raw_badges)
            
            # Use robust name extraction
            final_user = get_best_name(tags, prefix)
            
            # Process Emotes
            raw_emotes = tags.get("emotes")
            # print(f"[Parser Debug] Pre-Emote Text: '{text}' Emotes: {raw_emotes}")
            text = process_twitch_emotes(text, raw_emotes)
            # print(f"[Parser Debug] Post-Emote Text: '{text}'")
            
            # Normal Chat
            subscriber_flag = False
            # Check subscriber flag in badges
            if "subscriber" in raw_badges or "founder" in raw_badges:
                subscriber_flag = True
            # There is also a distinct `subscriber` tag
            if tags.get("subscriber") == "1":
                subscriber_flag = True
                
            is_mod = tags.get("mod") == "1" or "moderator" in raw_badges or "broadcaster" in raw_badges
            is_vip = "vip" in raw_badges

            # Check custom-reward-id (IRC redemption)
            reward_id = tags.get("custom-reward-id")
            if reward_id:
                return "Twitch redeem (irc)", {
                    "trigger": f"Twitch redeem {reward_id[:8]}...",
                    "customData": {
                        "reward_id": reward_id,
                        "username": final_user,
                        "user": final_user, # COMPATIBILITY
                        "message": text,
                        "badges_html": final_badges,
                        "subscriber": subscriber_flag,
                        "mod": is_mod,
                        "vip": is_vip,
                        **base_custom_data
                    }
                }
            
            # Check bits
            bits = tags.get("bits")
            if bits:
                return "Twitch cheer", {
                    "trigger": f"Twitch cheer {bits} bits",
                    "customData": {
                        "bits": bits,
                        "username": final_user,
                        "user": final_user, # COMPATIBILITY
                        "message": text,
                        "badges_html": final_badges,
                        "subscriber": subscriber_flag,
                        "mod": is_mod,
                        "vip": is_vip,
                        **base_custom_data
                    }
                }

            return "Twitch chat", {
                "trigger": "Twitch chat",
                "customData": {
                    "username": final_user,
                    "user": final_user, # COMPATIBILITY
                    "message": text,
                    "badges_html": final_badges,
                    "color": tags.get("color", "#FFFFFF"),
                    "subscriber": subscriber_flag,
                    "mod": is_mod,
                    "vip": is_vip,
                    **base_custom_data
                }
            }
            
        # USERNOTICE (Subs, Raids)
        elif command == "USERNOTICE":
            msg_id = tags.get("msg-id", "")
            username = tags.get("display-name") or tags.get("login")
            
            # If this is a Stream Together shared notification, the real event is in source-msg-id
            if msg_id == "sharedchatnotice":
                msg_id = tags.get("source-msg-id", msg_id)
            
            if msg_id in ["sub", "resub", "subgift", "anonsubgift", "giftpaidupgrade", "submysterygift"]:
                plan = tags.get("msg-param-sub-plan", "")
                return "Twitch sub", {
                    "trigger": "Twitch sub",
                    "customData": {
                        "username": username,
                        "type": msg_id,
                        "plan": plan,
                        "message": text,
                        **base_custom_data
                    }
                }
            elif msg_id == "raid":
                count = tags.get("msg-param-viewerCount", "0")
                raider = tags.get("msg-param-login", username)
                return "Twitch raid", {
                    "trigger": "Twitch raid",
                    "customData": {
                        "username": raider,
                        "viewers": count,
                        **base_custom_data
                    }
                }
        
        elif command == "CLEARCHAT":
            # Ban/Timeout
            target_user = parts[1] if len(parts) > 1 else ""
            if "ban-duration" in tags:
                return "Twitch timeout", { "trigger": "Twitch timeout", "customData": {"username": target_user, "duration": tags["ban-duration"], **base_custom_data} }
            else:
                return "Twitch ban", { "trigger": "Twitch ban", "customData": {"username": target_user, **base_custom_data} }

        # Filter out noisy IRC protocol commands before falling back to "other"
        noisy_commands = {"JOIN", "PART", "PING", "PONG", "CAP", "HOSTTARGET", "RECONNECT", 
                          "353", "366", "001", "002", "003", "004", "375", "372", "376", 
                          "USERSTATE", "ROOMSTATE", "GLOBALUSERSTATE"}
        if command in noisy_commands:
            return None

        return "Twitch other", {
            "trigger": f"Twitch other ({command})",
            "customData": {"command": command, "raw": line, **base_custom_data}
        }

# --- Twitch Badge Logic ---
def fetch_twitch_badges_gql():
    print("[Twitch] Fetching GLOBAL badges via GQL...")
    gql_url = "https://gql.twitch.tv/gql"
    query = {
        "query": "query GetGlobalBadges { badges { setID version imageURL } }"
    }
    client_id = "kimne78kx3ncx6brgo4mv6wki5h1ko" # Public web client ID
    headers = {"Client-Id": client_id, "User-Agent": "Mozilla/5.0"}

    try:
        req = urllib.request.Request(gql_url, data=json.dumps(query).encode('utf-8'), headers=headers, method='POST')
        with urllib.request.urlopen(req) as resp:
            data = json.load(resp)
            badges = data.get("data", {}).get("badges", [])
            for b in badges:
                sid = b.get("setID")
                ver = b.get("version")
                url_1x = b.get("imageURL")
                
                # Upgrade to 2x for better quality if possible
                # Usually standard URLs assume .../1 for 1x, we want /2
                url = url_1x
                if url and url.endswith("/1"):
                     url = url[:-2] + "/2"

                if sid and ver and url:
                    if sid not in BADGE_DB: BADGE_DB[sid] = {}
                    BADGE_DB[sid][ver] = url
            print(f"[Twitch] Loaded {len(badges)} badge sets via GQL.")
    except Exception as e:
        print(f"[Twitch] GQL Badge fetch failed: {e}")

def load_badges():
    # Run in background to not block startup
    t = threading.Thread(target=fetch_twitch_badges_gql, daemon=True)
    t.start()

def process_twitch_badges(badges_str):
    if not badges_str: return ""
    html = ""
    
    # Map Twitch badge keys to Overlay shortcodes (for internal SVGs)
    KEY_MAP = {
        "subscriber": "SUB",
        "broadcaster": "BROADCASTER",
        "moderator": "MOD",
        "vip": "VIP",
        "founder": "FOUNDER",
        "partner": "VERIFIED",
        "glitchcon2020": "OG", # Example mapping
        "premium": "VIP"
    }

    for item in badges_str.split(","):
        if "/" in item:
            key, val = item.split("/", 1)
            # 1. Try Cache
            url = BADGE_DB.get(key, {}).get(val)
            # 2. Fallback to standard HTTP URL guess if we want, or just skip
            if not url:
                # Fallback purely for standard ones if GQL failed
                url = f"https://static-cdn.jtvnw.net/badges/v1/{val}/2" if key in ["broadcaster", "moderator"] else ""
            
            if url:
                html += f'[badge:{url}:{key}]'
            else:
                # Use mapped key if available (e.g., [SUB]) else [subscriber]
                short_code = KEY_MAP.get(key, key.upper())
                html += f'[{short_code}]'
    return html

def process_twitch_emotes(text, emotes_tag):
    if not emotes_tag or not text: return text
    
    # Parse emotes tag: id:start-end,start-end/id2:start-end
    # Example: 25:0-4,12-16/1902:6-10
    replacements = []
    
    try:
        for emote_group in emotes_tag.split("/"):
            if ":" not in emote_group: continue
            eid, positions = emote_group.split(":")
            for pos in positions.split(","):
                if "-" not in pos: continue
                start, end = map(int, pos.split("-"))
                # end is inclusive in Twitch tags, so end+1 for python slice
                replacements.append((start, end + 1, eid))
    except:
        return text

    # Sort descending by start to avoid index shifting issues
    replacements.sort(key=lambda x: x[0], reverse=True)
    
    # Apply replacements
    result = list(text) # convert to list of chars for easy mutability? 
    # Actually, slicing strings is easier if we rebuild
    # But since we go backwards, we can just slice.
    
    for start, end, eid in replacements:
        # Check bounds
        if start < 0 or end > len(text): continue
        
        replacement_text = f"[twitch_emote:{eid}]"
        # original_text = text[start:end]
        text = text[:start] + replacement_text + text[end:]
        
    return text

# --- Twitch Driver ---
async def get_twitch_channel_id(session, username):
    client_id = "kimne78kx3ncx6brgo4mv6wki5h1ko"
    gql_url = "https://gql.twitch.tv/gql"
    query = {
        "query": "query($login: String!) { user(login: $login) { id } }",
        "variables": {"login": username}
    }
    try:
        headers = {"Client-Id": client_id}
        async with session.post(gql_url, json=query, headers=headers, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                user_data = data.get("data", {}).get("user")
                if user_data and user_data.get("id"):
                    return user_data['id']
    except Exception as e:
        print(f"[Twitch] GQL ID fetch failed: {e}")
    return None

AVATAR_CACHE = {}
KICK_AVATAR_CACHE = {}
YOUTUBE_AVATAR_CACHE = {}

async def get_youtube_channel_avatar(session, video_url):
    if not video_url:
        return None
    if video_url in YOUTUBE_AVATAR_CACHE:
        return YOUTUBE_AVATAR_CACHE[video_url]
    
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        async with session.get(video_url, headers=headers, timeout=10) as resp:
            if resp.status == 200:
                html = await resp.text()
                import re
                m = re.search(r'\"videoOwnerRenderer\"\:\{\"thumbnail\"\:\{\"thumbnails\"\:\[\{\"url\"\:\"([^\"]+)\"', html)
                if m:
                    url = m.group(1).replace("\\u0026", "&")
                    url = re.sub(r'=s\d+-', '=s88-', url)
                    YOUTUBE_AVATAR_CACHE[video_url] = url
                    return url
    except Exception as e:
        print(f"[YouTube] Avatar fetch error: {e}")
    return None

async def get_twitch_user_avatar_from_id(session, user_id):
    if not user_id:
        return None, None
    if user_id in AVATAR_CACHE:
        return AVATAR_CACHE[user_id]
        
    client_id = "kimne78kx3ncx6brgo4mv6wki5h1ko"
    gql_url = "https://gql.twitch.tv/gql"
    query = {
        "query": "query($id: ID!) { user(id: $id) { login profileImageURL(width: 70) } }",
        "variables": {"id": user_id}
    }
    try:
        headers = {"Client-Id": client_id}
        async with session.post(gql_url, json=query, headers=headers, timeout=5) as resp:
            if resp.status == 200:
                data = await resp.json()
                user_data = data.get("data", {}).get("user")
                if user_data:
                    login = user_data.get("login")
                    url = user_data.get("profileImageURL")
                    if login and url:
                        AVATAR_CACHE[user_id] = (login, url)
                        return login, url
    except Exception as e:
        print(f"[Twitch] GQL Avatar fetch failed: {e}")
    return None, None

async def get_reward_title_gql(session, channel_id, reward_id):
    client_id = "kimne78kx3ncx6brgo4mv6wki5h1ko"
    gql_url = "https://gql.twitch.tv/gql"
    query = {
        "query": "query GetChannelRewards($channelID: ID!) { channel(id: $channelID) { communityPointsSettings { customRewards { id title } } } }",
        "variables": {"channelID": channel_id}
    }
    try:
        headers = {"Client-Id": client_id}
        async with session.post(gql_url, json=query, headers=headers, timeout=5) as resp:
            if resp.status == 200:
                data = await resp.json()
                rewards = data.get("data", {}).get("channel", {}).get("communityPointsSettings", {}).get("customRewards", [])
                for r in rewards:
                    if r.get("id") == reward_id:
                        return r.get("title")
    except:
        pass
    return "Unknown"

async def monitor_twitch_pubsub(session, channel_name, channel_id):
    client_id = "kimne78kx3ncx6brgo4mv6wki5h1ko"
    # Using the exact URL found by Spy
    ws_url = f"wss://hermes.twitch.tv/v1?clientId={client_id}"
    
    # Subscribe to multiple topics to mimic browser behavior and keep connection stable
    # The Spy tool confirmed these are used by the real Twitch client
    topics = [
        f"community-points-channel-v1.{channel_id}", # MAIN GOAL: Redemptions
        f"video-playback-by-id.{channel_id}",        # Viewer count / stream status
        f"raid.{channel_id}",                         # Raids
        f"polls.{channel_id}",                        # Polls
        f"predictions-channel-v1.{channel_id}",       # Predictions
        f"hype-train-events-v1.{channel_id}"          # Hype Train
    ]

    def generate_nonce(length=22):
        chars = string.ascii_letters + string.digits + "_-"
        return ''.join(random.choice(chars) for _ in range(length))

    backoff = 1
    while True:
        try:
            print(f"[Twitch PubSub] Connecting to Hermes for {channel_name}...")
            # Headers found in Spy
            headers = {
                "Origin": "https://www.twitch.tv", 
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
            }
            async with session.ws_connect(ws_url, headers=headers, heartbeat=15) as ws:
                print(f"[Twitch PubSub] Connected! Subscribing to {len(topics)} topics...")
                
                # Send subscriptions for all topics
                for topic in topics:
                    sub_msg = {
                        "type": "subscribe",
                        "id": generate_nonce(),
                        "subscribe": {
                            "id": generate_nonce(),
                            "type": "pubsub",
                            "pubsub": {"topic": topic}
                        },
                        "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + "Z"
                    }
                    await ws.send_json(sub_msg)
                    # Small delay to avoid flooding
                    await asyncio.sleep(0.1)
                
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        payload = msg.data
                        # if "pong" not in payload.lower() and "ping" not in payload.lower() and "keepalive" not in payload.lower():
                        #      # Log interesting packets to help debug "lost" redemptions
                        #      if len(payload) < 2000:
                        #         print(f"[Twitch PubSub RAW] {payload}")
                        #      else:
                        #         print(f"[Twitch PubSub RAW] (Large payload: {len(payload)} chars)")
                        
                        try:
                            data = json.loads(payload)
                        except:
                            continue

                        # Handle Notifications
                        # Twitch Hermes/PubSub often wraps the actual payload in data.message as a string
                        # Pass ALL JSON payloads to the parser
                        # The parser's find_redemption will handle nesting, or it will return a generic event.
                        res = TwitchParser.parse_frame(json.dumps(data))
                        if res:
                                ek, fmt = res
                                # Resolve unknown title
                                if ek == "Twitch redeem (pubsub)" and fmt["customData"].get("reward_title") == "Unknown":
                                    rid = fmt["customData"].get("reward_id")
                                    if rid:
                                        rt = await get_reward_title_gql(session, channel_id, rid)
                                        if rt != "Unknown":
                                            fmt["customData"]["reward_title"] = rt
                                            fmt["trigger"] = f"Twitch redeem {rt}"

                                event_queue.put(("TwitchParser", channel_name, ek, fmt["trigger"], fmt["customData"]))
        except Exception as e:
            print(f"[Twitch PubSub] Error: {e}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

async def monitor_twitch_irc(channel):
    host = "irc.chat.twitch.tv"
    port = 6667
    
    nick = "justinfan123456"
    pwd = "oauth:123123123"
    
    backoff = 1
    while True:
        try:
            reader, writer = await asyncio.open_connection(host, port)
            
            writer.write(b"CAP REQ :twitch.tv/tags twitch.tv/commands\r\n")
            writer.write(f"CAP REQ :twitch.tv/tags twitch.tv/commands twitch.tv/membership\r\n".encode())
            writer.write(f"PASS {pwd}\r\n".encode())
            writer.write(f"NICK {nick}\r\n".encode())
            writer.write(f"JOIN #{channel}\r\n".encode())
            await writer.drain()
            
            print(f"[Twitch IRC] Connected to {channel} as {nick}")
            backoff = 1

            while True:
                line_bytes = await reader.readline()
                if not line_bytes: break
                line = line_bytes.decode("utf-8", errors="ignore").strip()
                
                if line.startswith("PING"):
                    writer.write(b"PONG :tmi.twitch.tv\r\n")
                    await writer.drain()
                    continue

                if "WHISPER" in line:
                    try:
                        # Example: @badges=...;... :user!user@user.tmi.twitch.tv WHISPER target :message
                        # Or plain: :user!user@user.tmi.twitch.tv WHISPER target :message
                        
                        raw_msg = line
                        tags = {}
                        if raw_msg.startswith("@"):
                            parts = raw_msg.split(" ", 1)
                            tag_str = parts[0][1:]
                            raw_msg = parts[1]
                            for t in tag_str.split(";"):
                                k, v = t.split("=", 1) if "=" in t else (t, "")
                                tags[k] = v

                        parts = raw_msg.split(" WHISPER ", 1)
                        if len(parts) > 1:
                            user_str = parts[0]
                            # :user!user@...
                            sender = user_str.split("!", 1)[0]
                            if sender.startswith(":"): sender = sender[1:]
                            
                            rest = parts[1]
                            # target :message
                            # Wait, WHISPER target :message
                            target, message = rest.split(" :", 1) if " :" in rest else (rest, "")
                            
                            event_queue.put(("TwitchParser", target.strip(), "Twitch whisper", f"Whisper from {sender}", {
                                "username": sender,
                                "message": message,
                                "type": "whisper",
                                "badges_html": "",  # TODO: process badges if needed
                                "color": tags.get("color", "#FF0000") # Use Red for whispers?
                            }))
                    except:
                        pass
                
                if "PRIVMSG" in line:
                    pass # print(f"[Twitch RAW] {line}")

                res = TwitchParser.parse_frame(line)
                if res:
                    ek, fmt = res
                    event_queue.put(("TwitchParser", channel, ek, fmt["trigger"], fmt["customData"]))
        
        except Exception as e:
             print(f"[Twitch IRC] Error: {e}")
             await asyncio.sleep(backoff)
             backoff = min(backoff * 2, 60)

async def start_twitch_monitor(session, username):
    # 1. Start IRC
    asyncio.create_task(monitor_twitch_irc(username))
    
    # 2. Resolve ID for PubSub
    cid = await get_twitch_channel_id(session, username)
    if cid:
        # User requested to disable direct PubSub connection (Hermes) as it was deemed incorrect/unstable
        # in previous findings. Relying on IRC tags for redemptions instead.
        print(f"[Twitch] Enabling PubSub Monitor for {username} (ID: {cid})")
        asyncio.create_task(monitor_twitch_pubsub(session, username, cid))
    else:
        print(f"[Twitch] Could not resolve ID for {username}, PubSub (Redemptions) unavailable.")


# ==============================================================================
# 3. KICK LOGIC
# ==============================================================================

class KickParser:
    INPUT_TYPE = "username"
    EVENTS = [
        "Kick chat", "Kick redeem", "Kick follow", "Kick sub", "Kick gift sub",
        "Kick raid start", "Kick raid end", "Kick ban", "Kick timeout", 
        "Kick stream start", "Kick stream end", "Kick other"
    ]
    TRIGGERS = {
        "Kick chat":         "Kick chat",
        "Kick redeem":       "Kick redeem {title}",
        "Kick follow":       "Kick follow",
        "Kick sub":          "Kick sub",
        "Kick gift sub":     "Kick gift sub",
        "Kick raid start":   "Kick raid start",
        "Kick raid end":     "Kick raid end",
        "Kick ban":          "Kick ban",
        "Kick timeout":      "Kick timeout",
        "Kick stream start": "Kick stream start",
        "Kick stream end":   "Kick stream end",
        "Kick other":        "Kick other ({event})"
    }

    @staticmethod
    def try_json(s):
        try: return json.loads(s)
        except: return None

    @staticmethod
    def detect_event_name(payload_str):
        names = [
            "ChatMessageEvent", "RewardRedeemedEvent", "FollowEvent", "SubscriptionEvent",
            "GiftedSubscriptionEvent", "PinnedMessageEvent", "ReactionCreatedEvent",
            "UserBannedEvent", "UserTimedOutEvent", "StreamStartedEvent", "StreamEndedEvent",
            "HostStartedEvent", "HostEndedEvent", "RaidStartedEvent", "RaidEndedEvent",
            "PollStartedEvent", "PollEndedEvent", "PollVoteEvent", "StreamUpdatedEvent",
            "ChatClearedEvent", "EmoteCreatedEvent", "EmoteDeletedEvent"
        ]
        for n in names:
            if n in payload_str: return n
        return None

    @staticmethod
    def parse_frame(payload_str):
        # 1. Detect Event Name (simple substring check first)
        en = KickParser.detect_event_name(payload_str)
        if not en:
            # Fallback or check if it's a pusher internal event
            return None

        # 2. Parse JSON
        d = KickParser.try_json(payload_str) or {}
        # data often needs double decoding
        raw_data = d.get("data")
        if isinstance(raw_data, str):
            inner = KickParser.try_json(raw_data)
            if isinstance(inner, dict):
                d["data"] = inner
        
        # Prepare Output
        ek = "Kick other"
        title = ""
        payload = d.get("data", {})
        if not isinstance(payload, dict): payload = {"raw": payload}

        if en == "ChatMessageEvent":
            ek = "Kick chat"
            # Normalize payload for overlay
            sender = payload.get("sender", {})
            payload["username"] = sender.get("username", "Unknown")
            payload["user"] = sender.get("username", "Unknown") # COMPATIBILITY
            payload["message"] = payload.get("content", "")
            payload["color"] = sender.get("identity", {}).get("color", "#CCCCCC")
            
            # Kick Badges Processing
            # badges items are usually: {"type": "broadcaster", "active": true}
            badges_list = sender.get("identity", {}).get("badges", [])
            badge_str = ""
            is_sub = False
            is_mod = False
            is_vip = False
            
            for b in badges_list:
                # Some payloads have badges as simple list of strings, some as dicts
                b_type = ""
                if isinstance(b, dict):
                    if b.get("active", True): # Assume active if key missing
                        b_type = b.get("type", "")
                elif isinstance(b, str):
                    b_type = b
                
                if b_type:
                    # Map to Overlay format [BROADCASTER], [MOD], [SUB]
                    if "broadcaster" in b_type: 
                        badge_str += "[BROADCASTER]"
                        is_mod = True
                    elif "moderator" in b_type: 
                        badge_str += "[MOD]"
                        is_mod = True
                    elif "subscriber" in b_type: 
                        badge_str += "[SUB]"
                        is_sub = True
                    elif "vip" in b_type: 
                        badge_str += "[VIP]"
                        is_vip = True
                    elif "founder" in b_type: 
                        badge_str += "[FOUNDER]"
                        is_sub = True
                    elif "og" in b_type: badge_str += "[OG]"
                    else:
                        pass # Ignore unknown badges or use generic [b_type.upper()]
            
            payload["badges"] = badge_str
            payload["subscriber"] = is_sub
            payload["mod"] = is_mod
            payload["vip"] = is_vip

        elif en == "RewardRedeemedEvent":
            ek = "Kick redeem"
            # Normalize title
            title = (payload.get("reward", {}) or {}).get("title") or payload.get("reward_title") or payload.get("title") or "Unknown"

        elif en == "FollowEvent": ek = "Kick follow"
        elif en == "SubscriptionEvent": ek = "Kick sub"
        elif en == "GiftedSubscriptionEvent": ek = "Kick gift sub"
        elif en == "RaidStartedEvent": ek = "Kick raid start"
        elif en == "RaidEndedEvent": ek = "Kick raid end"
        elif en == "UserBannedEvent": ek = "Kick ban"
        elif en == "UserTimedOutEvent": ek = "Kick timeout"
        elif en == "StreamStartedEvent": ek = "Kick stream start"
        elif en == "StreamEndedEvent": ek = "Kick stream end"

        # Filter out noisy Kick events that spam SAMMI endlessly
        if en in ["ReactionCreatedEvent", "PollVoteEvent", "StreamUpdatedEvent"]:
            return None

        trigger = KickParser.TRIGGERS.get(ek, KickParser.TRIGGERS["Kick other"]).format(title=title, event=en)

        return ek, {
            "trigger": trigger,
            "customData": payload
        }

# To avoid overly long file I will assume Kick logic is minimal or handled by generic parser
# But I must implement the Driver for Kick which uses Pusher.

async def get_kick_metadata(username):
    # Fetch chatroom ID
    chat_id, chan_id = None, None
    print(f"[Kick] Fetching metadata for {username}...")
    try:
        # Use a new session to avoid sharing issues if any, but ideally use passed session
        url = f"https://kick.com/api/v1/channels/{username}"
        
        async def fetch_with_curl():
            import subprocess
            try:
                # Windows 10+ has curl natively
                res = await asyncio.to_thread(subprocess.run, ["curl", "-s", "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36", url], capture_output=True, text=True, timeout=10)
                if res.returncode == 0 and res.stdout.startswith('{'):
                    return json.loads(res.stdout)
            except Exception as e:
                pass
            return None

        async with aiohttp.ClientSession() as sess:
            headers = {
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                 "Accept": "application/json",
                 "Accept-Language": "en-US,en;q=0.9",
            }
            d = None
            async with sess.get(url, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    d = await resp.json()
                elif resp.status in [403, 401]:
                    print(f"[Kick] API Error {resp.status} - Falling back to curl...")
                    d = await fetch_with_curl()
                else:
                    print(f"[Kick] API Error {resp.status}")
            
            if d:
                chan_id = d.get("id")
                if "chatroom" in d: chat_id = d["chatroom"]["id"]
                if "user" in d and "profile_pic" in d["user"]:
                    KICK_AVATAR_CACHE[username] = d["user"]["profile_pic"]
    except Exception as e:
        print(f"[Kick] Metadata Fetch Error: {e}")
        
        # Super fallback
        if not chat_id:
            try:
                import subprocess
                res = await asyncio.to_thread(subprocess.run, ["curl", "-s", "-A", "Mozilla/5.0", f"https://kick.com/api/v1/channels/{username}"], capture_output=True, text=True, timeout=10)
                if res.returncode == 0 and res.stdout.startswith('{'):
                    d = json.loads(res.stdout)
                    chan_id = d.get("id")
                    if "chatroom" in d: chat_id = d["chatroom"]["id"]
                    if "user" in d and "profile_pic" in d["user"]:
                        KICK_AVATAR_CACHE[username] = d["user"]["profile_pic"]
            except:
                pass
    
    if chat_id:
        print(f"[Kick] Resolved {username}: ChatID={chat_id}, ChanID={chan_id}")
    else:
        print(f"[Kick] Failed to resolve {username}.")
        
    return chat_id, chan_id

async def monitor_kick(session, username):
    chat_id, chan_id = await get_kick_metadata(username)
    if not chat_id:
        print(f"[Kick] Failed to find chatroom for {username}")
        return

    # Connect to Pusher
    ws_url = "wss://ws-us2.pusher.com/app/32cbd69e4b950bf97679?protocol=7&client=js&version=8.4.0&flash=false"
    
    backoff = 1
    while True:
        try:
            async with session.ws_connect(ws_url) as ws:
                print(f"[Kick] Connected for {username}")
                # Subscribe
                channels = [
                    f"chatrooms.{chat_id}.v2", 
                    f"channel.{chan_id}", 
                    f"channel_{chan_id}", 
                    f"chatroom_{chat_id}", 
                    f"chatrooms.{chat_id}"
                ]
                for c in channels:
                     await ws.send_json({"event": "pusher:subscribe", "data": {"auth": "", "channel": c}})
                
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        res = KickParser.parse_frame(msg.data)
                        if res:
                            ek, fmt = res
                            event_queue.put(("KickParser", username, ek, fmt["trigger"], fmt["customData"]))
        except Exception as e:
            print(f"[Kick] Error: {e}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


# ==============================================================================
# 4. YOUTUBE LOGIC (pytchat)
# ==============================================================================
import logging
# Suppress pytchat internal logging
logging.getLogger("pytchat").setLevel(logging.CRITICAL)

try:
    import pytchat
    HAS_PYTCHAT = True
except ImportError:
    HAS_PYTCHAT = False
    print("[YouTube] pytchat not installed. YouTube support disabled.")

class YouTubeParser:
    INPUT_TYPE = "url"
    EVENTS = ["chat_message", "paid_message", "sticker_message", "member_message", "gift_message"]
    # Pytchat handles parsing, so we just map its objects to our Dict format

async def monitor_youtube_pytchat(input_url):
    if not HAS_PYTCHAT: return

    # pytchat can take a video URL OR a channel URL (if live).
    # For channel URL, we might need to find the video ID first, but pytchat.create can sometimes handle it.
    # PRO TIP: pytchat.create(video_id=...) is robust.
    
    # Extract ID if Studio URL
    target_id = None
    
    try:
        parsed = urllib.parse.urlparse(input_url)
        if "v" in urllib.parse.parse_qs(parsed.query):
            target_id = urllib.parse.parse_qs(parsed.query)["v"][0]
            print(f"[YouTube] Extracted Video ID: {target_id} from URL")
        else:
            # If no v param, try to handle or just fail
             print(f"[YouTube] URL {input_url} does not contain video ID. Pytchat might fail.")
             # pytchat really wants a video_id matching a video
    except:
        pass
    
    print(f"[YouTube] Starting pytchat for {target_id}")

    if not target_id:
         return 
    
    # We'll run pytchat in a thread since it's synchronous blocking or has its own internal loop.
    # Actually pytchat.create() returns a LiveChat object that we can poll.
    
    try:
        chat = pytchat.create(video_id=target_id, interruptable=False)
    except Exception as e:
        print(f"[YouTube] Pytchat create failed: {e}")
        return
    
    while True:
        try:
            if not chat.is_alive():
                # Try to reconnect or check if stream ended
                await asyncio.sleep(5)
                # Re-create loop?
                # For now just continue
                continue

            # Check for new data
            # chat.get() returns a list of sync Chat objects
            # We wrap this in a thread executor to not block asyncio loop
            data = await asyncio.to_thread(chat.get)
            
            # Pytchat compatibility
            items = data.items if hasattr(data, "items") else data
            
            for c in items:
                # c is a pytchat Message object
                # Attributes: author, message, amountString, money.text, type, etc.
                
                # 1. Super Chat
                if c.amountString:
                    # Paid message
                    ek = "paid_message"
                    trigger = f"Super Chat: {c.amountString}"
                    if c.type == "superSticker":
                        ek = "sticker_message"
                        trigger = f"Super Sticker: {c.amountString}"
                    
                    event_queue.put(("YouTubeParser", input_url, ek, trigger, {
                        "author": c.author.name,
                        "amount": c.amountString,
                        "message": c.message,
                        "image": c.author.imageUrl
                    }))
                    continue
                
                # 2. New Message
                # pytchat types: textMessage, superChat, superSticker, newSponsor, etc.
                if c.type == "newSponsor":
                     event_queue.put(("YouTubeParser", input_url, "member_message", "New Member!", {
                         "author": c.author.name
                     }))
                     continue

                # 3. Standard Chat
                # Construct message with emotes if available
                final_message = c.message
                if hasattr(c, 'messageEx') and c.messageEx:
                    try:
                        final_message = ""
                        for fragment in c.messageEx:
                            if isinstance(fragment, str):
                                final_message += fragment
                            elif isinstance(fragment, dict):
                                if 'url' in fragment:
                                    final_message += f"[yt_emoji:{fragment['url']}]"
                                else:
                                    final_message += str(fragment.get('txt', ''))
                    except Exception as e:
                        print(f"[YouTube] Emote parsing error: {e}")
                        final_message = c.message

                # If we filter standard chat:
                event_queue.put(("YouTubeParser", input_url, "chat_message", "YouTube Chat", {
                    "username": c.author.name,  # Unified with other platforms
                    "author": c.author.name,    # Keeping for backwards compatibility
                    "user_id": getattr(c.author, "channelId", ""),
                    "message": final_message,
                    "badges": getattr(c.author, "badgeUrl", ""),
                    "image": getattr(c.author, "imageUrl", ""),
                    "id": getattr(c, "id", ""), # Helps with banning
                    "subscriber": getattr(c.author, "isChatSponsor", False),
                    "mod": getattr(c.author, "isChatOwner", False) or getattr(c.author, "isChatModerator", False),
                    "vip": getattr(c.author, "isVerified", False)
                }))

            # Cache Live ID if possible (pytchat doesn't expose it easily, but we use input_url)
            # We skip this for now as YT sending is experimental.
            
            await asyncio.sleep(0.5)

        except Exception as e:
            print(f"[YouTube] Error: {e}")
            await asyncio.sleep(5)

# ==============================================================================
# 5. MAIN APPLICATION
# ==============================================================================

async def main():
    print("=== StreamerAlertRelay Unified (v2.0) ===")
    
    # 1. Load Settings
    load_sammi_settings()
    load_sub_cache()
    load_badges()
    
    # 1a. Start TTS Thread
    
    # 2. Start WS Server
    start_ws_server()
    
    # 2a. Print UI Instructions
    overlay_path = os.path.join(BASE_DIR, "chat_overlay.html")

    print(f"\n[UI] Message Overlay is available at:\n     file://{overlay_path}")
    print("     (Open in Browser or add as OBS Browser Source)\n")
    
    # 3. Load Config (Unified)
    global GLOBAL_CONFIG
    GLOBAL_CONFIG = load_complete_config_state()
    config_state = GLOBAL_CONFIG

    # Update SAMMI Globals from loaded config
    global SAMMI_URL, SAMMI_PASS
    SAMMI_URL = config_state["sammi"].get("sammi_url", SAMMI_URL)
    SAMMI_PASS = config_state["sammi"].get("sammi_password", None)

    print(f"[SAMMI] URL: {SAMMI_URL}")

    chats = config_state["chats"]
    if not chats:
        print("[WARN] No chats configured.")

    # 4. Start Drivers
    session = aiohttp.ClientSession()
    tasks = []

    # Create a persistent background task for each parser
    # CRITICAL: We must ensure they don't exit prematurely or block the loop.
    # Twitch requires TWO connections (IRC + PubSub) which must run concurrently.
    # Kick requires a persistent WebSocket (Pusher).
    # YouTube runs a polling loop (Pytchat).
    # DO NOT remove the wrappers or fire-and-forget these tasks. They must be tracked in 'tasks'.
    
    for chat_id, chat_data in chats.items():
        parser_name = chat_data.get("parser")
        inp = chat_data.get("input")
        
        if not inp: continue
        
        print(f"[{chat_id}] Launching {parser_name} for '{inp}'")
        
        if parser_name == "twitch_parse":
            # We must track the sub-tasks created by start_twitch_monitor
            async def _start_twitch_wrapper(s, u):
                # This wrapper keeps the task alive by waiting for the sub-monitors
                # Start IRC
                t_irc = asyncio.create_task(monitor_twitch_irc(u))
                t_pub = None
                
                # Fetch ID for PubSub
                cid = await get_twitch_channel_id(s, u)
                if cid:
                    print(f"[Twitch] Enabling PubSub Monitor for {u} (ID: {cid})")
                    t_pub = asyncio.create_task(monitor_twitch_pubsub(s, u, cid))
                else:
                    print(f"[Twitch] Could not resolve ID for {u}, PubSub unavailable.")
                
                # Wait for both
                todo = [t_irc]
                if t_pub: todo.append(t_pub)
                
                try:
                    await asyncio.gather(*todo)
                except asyncio.CancelledError:
                    for t in todo: t.cancel()
                except Exception as e:
                    print(f"[Twitch Wrapper] Error: {e}")

            tasks.append(asyncio.create_task(_start_twitch_wrapper(session, inp)))
        
        elif parser_name == "kick_parse":
             # Passed session is used for Kick API & Pusher
             tasks.append(asyncio.create_task(monitor_kick(session, inp)))
             
        elif parser_name == "youtube_parse":
             tasks.append(asyncio.create_task(monitor_youtube_pytchat(inp)))

    print("=== Monitoring Started. Press Ctrl+C or use Tray Icon to stop. ===")

    # 5. Event Loop (Process Queue)
    while not STOP_EVENT.is_set():
        global RELOAD_CONFIG_PENDING
        if RELOAD_CONFIG_PENDING:
            RELOAD_CONFIG_PENDING = False
            print("[System] Reloading configuration and restarting chat listeners...")
            for t in tasks:
                t.cancel()
            tasks.clear()
            if not session.closed:
                await session.close()
            
            # 3. Load Config (Unified)
            GLOBAL_CONFIG = load_complete_config_state()
            config_state = GLOBAL_CONFIG
            
            # Update SAMMI Globals from loaded config
            SAMMI_URL = config_state["sammi"].get("sammi_url", SAMMI_URL)
            SAMMI_PASS = config_state["sammi"].get("sammi_password", None)
            
            chats = config_state["chats"]
            
            session = aiohttp.ClientSession()
            for chat_id, chat_data in chats.items():
                parser_name = chat_data.get("parser")
                inp = chat_data.get("input")
                if not inp: continue
                
                print(f"[{chat_id}] Relaunching {parser_name} for '{inp}'")
                if parser_name == "twitch_parse":
                    async def _start_twitch_wrapper(s, u):
                        t_irc = asyncio.create_task(monitor_twitch_irc(u))
                        t_pub = None
                        cid = await get_twitch_channel_id(s, u)
                        if cid:
                            t_pub = asyncio.create_task(monitor_twitch_pubsub(s, u, cid))
                        
                        todo = [t_irc]
                        if t_pub: todo.append(t_pub)
                        try:
                            await asyncio.gather(*todo)
                        except asyncio.CancelledError:
                            for t in todo: t.cancel()
                        except Exception as e:
                            print(f"[Twitch Wrapper] Error: {e}")
                    tasks.append(asyncio.create_task(_start_twitch_wrapper(session, inp)))
                elif parser_name == "kick_parse":
                    tasks.append(asyncio.create_task(monitor_kick(session, inp)))
                elif parser_name == "youtube_parse":
                    tasks.append(asyncio.create_task(monitor_youtube_pytchat(inp)))

        try:
            # Non-blocking get
            try:
                # Use a very small timeout for the blocking call to allow checking STOP_EVENT
                # But we must yield to the event loop if empty!
                item = event_queue.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.1) # Yield to other tasks!
                continue
            
            parser_name, channel, event, trigger, data = item

            msg_text = data.get("message", "")
            username = data.get("username", data.get("author", ""))
            # Remove leading @ from username if it exists (very common in Youtube/Kick)
            if username.startswith("@"):
                username = username[1:]
                
            # --- STANDARDIZE TRIGGER FORMAT ---
            # Format: [platform] [username] [type] {additional}
            if "twitch" in parser_name.lower(): plat = "Twitch"
            elif "kick" in parser_name.lower(): plat = "Kick"
            elif "youtube" in parser_name.lower(): plat = "YouTube"
            else: plat = "Unknown"

            disp_user = channel if channel and channel != "Unknown" else "System"
            if data.get("is_shared_chat"):
                disp_user = "ST"

            base_type = event
            additional = ""
            
            if plat == "Twitch":
                if event.startswith("Twitch "):
                    base_type = event[7:].replace(" (irc)", "").replace(" (pubsub)", "")
                
                if base_type == "cheer":
                    additional = trigger.replace("Twitch cheer ", "").strip()
                elif base_type == "redeem":
                    additional = trigger.replace("Twitch redeem ", "").strip()
                elif base_type == "hype train":
                    additional = trigger.replace("Twitch hype train ", "").strip()
                    
            elif plat == "Kick":
                if event.startswith("Kick "):
                    base_type = event[5:]
                    
                if base_type == "redeem":
                    additional = trigger.replace("Kick redeem ", "").strip()
                elif base_type == "raid start":
                    base_type = "raid"
                    
            elif plat == "YouTube":
                if event == "paid_message":
                    base_type = "super chat"
                    additional = trigger.replace("Super Chat: ", "").strip()
                elif event == "sticker_message":
                    base_type = "super sticker"
                    additional = trigger.replace("Super Sticker: ", "").strip()
                elif event == "member_message":
                    base_type = "member"
                elif event == "chat_message":
                    base_type = "chat"
                    
            trigger = f"{plat} {disp_user} {base_type}"
            if additional:
                trigger += f" {additional}"

            # --- FILTERING START ---
            # 1. Check if event is enabled in chat config
            # We need to find the chat for this parser/channel. 
            # Since channel/input can be ambiguous, we check all chats.
            is_allowed = True
            
            # Simple keyword ban (Legacy 'filters')
            # Assuming 'filters' in config are banned words
            banned_words = []
            
            # Event Type Filter (New 'event_filters')
            type_allowed = True
            chat_moderate_enabled = False
            chat_tts_enabled = True
            chat_sub_cache_enabled = False

            # Lookup Chat
            # This is slightly inefficient but config is small.
            found_chat = False
            for c_id, c_data in chats.items():
                cfg_parser = c_data.get("parser", "").lower().replace("_parse", "").replace("parser", "")
                evt_parser = parser_name.lower().replace("_parse", "").replace("parser", "")
                
                if cfg_parser == evt_parser:
                    # Match input.
                    # For youtube, strict match might fail if URL param normalized, but generally OK.
                    # For twitch/kick, input is username.
                    c_in = c_data.get("input", "")
                    if c_in and (c_in.lower() == channel.lower() or c_in in channel or channel in c_in):
                        found_chat = True
                        chat_moderate_enabled = c_data.get("moderate", False)
                        chat_tts_enabled = c_data.get("tts_enabled", True)
                        chat_sub_cache_enabled = c_data.get("sub_cache_enabled", False)
                        
                        # A. Event Type Check
                        ef = c_data.get("event_filters")
                        if not ef:
                            ef = c_data.get("filters") # Fallback to legacy
                            
                        if ef and isinstance(ef, dict):
                            # If event key exists in filters, respect it. Default True.
                            # Some events might have sub-types or different keys, 
                            # we check generic key 'event' (e.g. "Twitch chat")
                            if event in ef and not ef[event]:
                                type_allowed = False
                        
                        break
            
            if not type_allowed:
                continue

            # --- DYNAMIC SUBSCRIBER CACHING ---
            if chat_sub_cache_enabled and username and plat in SUB_DB:
                if channel not in SUB_DB[plat]:
                    SUB_DB[plat][channel] = {}
                cache = SUB_DB[plat][channel]
                user_lower = username.lower()
                is_sub_event = False
                if "subscriber" in data:
                    is_sub_event = bool(data["subscriber"])
                    if cache.get(user_lower) != is_sub_event:
                        cache[user_lower] = is_sub_event
                        threading.Thread(target=save_sub_cache, daemon=True).start()
                elif base_type in ["sub", "gift sub", "member"]:
                    if cache.get(user_lower) != True:
                        cache[user_lower] = True
                        threading.Thread(target=save_sub_cache, daemon=True).start()
                if "subscriber" not in data and user_lower in cache:
                    data["subscriber"] = cache[user_lower]
            
            if "subscriber" not in data:
                data["subscriber"] = "unknown"

            # Generate a temporary unique ID to link chat and moderation events
            import uuid
            msg_uuid = str(uuid.uuid4())
            
            # --- STREAM TOGETHER FILTERS ---
            st_filters = config_state.get("st_filters", {})
            st_tts_enabled = st_filters.get("tts", False)
            st_sammi_enabled = st_filters.get("sammi", False)
            st_overlay_enabled = st_filters.get("overlay", False)

            is_shared = data.get("is_shared_chat", False)
            print(f"[DEBUG-ROUTING] {event} from {username}. Is Shared: {is_shared}, st_tts_enabled: {st_tts_enabled}")

            allow_overlay = True
            if is_shared and not st_overlay_enabled:
                allow_overlay = False

            allow_sammi = True
            if is_shared and not st_sammi_enabled:
                allow_sammi = False

            allow_tts = True
            if is_shared and not st_tts_enabled:
                allow_tts = False

            # Broadcast to Overlay
            if allow_overlay: # Always broadcast, so shared chats with avatars show up
                if event in ["Twitch chat", "Kick chat", "chat_message", "Twitch whisper"]:
                    # --- CHAT ---
                    bc_type = "chat"
                    if "whisper" in event.lower(): 
                        bc_type = "whisper"
                    
                    bc_payload = {
                        "type": bc_type,
                        "trigger": event, # [ARCHITECTURAL NOTE] Trigger passed for websocket persistence explicitly
                        "msg_id": msg_uuid,
                        "source": parser_name.replace("Parser", "").replace("_parse", "").capitalize(),
                        "message": "",
                        "username": "Unknown",
                        "badges": ""
                    }
                    
                    # Unwrap Data based on Parser
                    if "Twitch" in parser_name or "twitch" in parser_name:
                         cd = data
                         bc_payload["message"] = cd.get("message", "")
                         bc_payload["username"] = cd.get("username", "Unknown")
                         bc_payload["badges"] = cd.get("badges_html", "") # Already in [badge:URL][MOD] format
                         bc_payload["color"] = cd.get("color", "")
                         
                         # Fetch the Channel Owner's avatar to replace the Twitch Company badge
                         # So if they stream together, they see WHICH Twitch channel it came from
                         c_id = cd.get("source_room_id") or cd.get("room_id")
                         if c_id:
                             login, url = await get_twitch_user_avatar_from_id(session, c_id)
                             if url:
                                 bc_payload["avatar"] = url

                    elif "Kick" in parser_name or "kick" in parser_name:
                         cd = data
                         bc_payload["message"] = cd.get("message", "")
                         bc_payload["username"] = cd.get("username", "Unknown")
                         # Kick badges handled in parser or here? 
                         # The parser usually adds [badge:...]
                         # But let's check
                         bc_payload["badges"] = cd.get("badges", "")
                         
                         # Add the avatar of the channel owner if cached
                         if channel in KICK_AVATAR_CACHE:
                             bc_payload["avatar"] = KICK_AVATAR_CACHE[channel]

                    elif "YouTube" in parser_name or "youtube" in parser_name:
                         # YouTube data is usually flat in 'data' for our queue
                         bc_payload["message"] = data.get("message", "")
                         bc_payload["username"] = data.get("author", "Unknown")
                         
                         # Instead of the chatter's avatar, fetch channel owner's avatar based on input_url (channel variable)
                         yt_avatar = await get_youtube_channel_avatar(session, channel)
                         if yt_avatar:
                             bc_payload["avatar"] = yt_avatar
                         else:
                             bc_payload["avatar"] = data.get("image", "")
    
                    broadcast(bc_payload)
                else:
                    # --- ALERT ---
                    broadcast({
                        "type": "alert",
                        "source": parser_name,
                        "event": event,
                        "trigger": trigger, # [ARCHITECTURAL NOTE] Trigger passed for websocket persistence explicitly
                        "data": data
                    })

            # Send to SAMMI
            platform_name = "twitch" if "twitch" in parser_name.lower() else "kick" if "kick" in parser_name.lower() else "youtube" if "youtube" in parser_name.lower() else "unknown"
            
            sammi_payload = {
                "trigger": trigger,
                "msg_id": msg_uuid,
                "platform": platform_name,
                "username": username,
                "message": msg_text,
                "customData": data
            }
            
            # --- AI MODERATION CHECK ---
            mod_config = config_state.get("moderation", {})
            is_suspicious = False
            vader_score = None
            
            if msg_text and chat_moderate_enabled:
            
                # 0. Fast Regex Link Check
                # Catches .com, .tv, http, www, and spaced out versions (e.g. youtube . com)
                if mod_config.get("regex_link_enabled", True):
                    url_pattern = re.compile(r"(?i)(?:https?://|www\.)|(?:\b[a-z0-9-]+\s*(?:\.|\bdot\b|\(\.\))\s*(?:com|net|org|tv|io|co|me|xyz)\b)")
                    if url_pattern.search(msg_text):
                        is_suspicious = True
                        sammi_payload["ai_reason"] = "Regex Link Filter"
                        print(f"[Moderation] Caught suspicious link via Regex: {msg_text}")
            
                # 1. Vader Check
                if not is_suspicious and mod_config.get("vader_enabled") and HAS_VADER and VADER_ANALYZER:
                    try:
                        scores = VADER_ANALYZER.polarity_scores(msg_text)
                        threshold = mod_config.get("vader_threshold", -0.5)
                        
                        # Add Vader score to the payload regardless of if it's suspicious
                        vader_score = scores['compound']
                        sammi_payload["vader_score"] = vader_score
                        
                        if vader_score <= threshold:
                            is_suspicious = True
                            sammi_payload["ai_reason"] = f"Vader Sentiment ({vader_score})"
                    except Exception as e:
                        print(f"[Vader] Error: {e}")

                # 2. Ollama Check (if not already flagged)
                if not is_suspicious and mod_config.get("ollama_enabled"):
                    try:
                        ollama_url = mod_config.get("ollama_url", "http://localhost:11434")
                        # Strip /api/generate if user left it from old config
                        base_url = ollama_url.replace("/api/generate", "")
                        ollama_model = mod_config.get("ollama_model", "llama3")
                        ollama_prompt = mod_config.get("ollama_prompt", "")
                        if not ollama_prompt.strip():
                            ollama_prompt = """You are an automated, strict chat moderation assistant for a live stream. Your ONLY job is to evaluate the provided chat message and respond with EXACTLY one word: "FLAGGED" or "CLEAN". Do not output any other text, punctuation, or explanation.

Flag the message (reply "FLAGGED") if it contains:
1. Hate Speech: Racism, sexism, homophobia, slurs, or discrimination.
2. Severe Toxicity: Direct threats, severe harassment, or telling people to harm themselves.
3. Harmful Spam: Repeated disruptive text block that ruins the chat experience. 

Do NOT flag the message (reply "CLEAN") if it is:
1. Normal conversation or questions.
2. Emote spam (e.g., "Kappa Kappa Kappa", "LUL LUL LUL").
3. Minor banter or harmless jokes.

### EXAMPLES ###
Message: "I hate you so much, kys" -> FLAGGED
Message: "Can someone help me with this boss?" -> CLEAN
Message: "PogChamp PogChamp PogChamp" -> CLEAN
Message: "ur actually so bad at this game lol" -> CLEAN"""
                        
                        
                        payload = {
                            "model": ollama_model,
                            "system": ollama_prompt,
                            "prompt": f"Message: \"{msg_text}\"",
                            "stream": False,
                            "options": {
                                "temperature": 0.1
                            }
                        }
                        
                        async with session.post(f"{base_url}/api/generate", json=payload) as resp:
                            if resp.status == 200:
                                ollama_data = await resp.json()
                                result_text = ollama_data.get("response", "").strip().upper()
                                if "FLAGGED" in result_text:
                                    is_suspicious = True
                                    sammi_payload["ai_reason"] = "Ollama Flagged"
                            else:
                                print(f"[Ollama] Error: HTTP {resp.status}")
                    except Exception as e:
                        print(f"[Ollama] Error: {e}")

                # Append the final status to the trigger
                status_tag = "FLAGGED" if is_suspicious else "CLEAN"
                if vader_score is not None:
                    sammi_payload["trigger"] = f"{sammi_payload['trigger']} {status_tag} {vader_score}"
                else:
                    sammi_payload["trigger"] = f"{sammi_payload['trigger']} {status_tag}"

            if is_suspicious:
                print(f"[MODERATION] Suspicious message detected from {username}: {msg_text}")

            # Ensure is_suspicious and moderate status persist in the generic payload for SAMMI explicitly
            sammi_payload["is_suspicious"] = is_suspicious
            sammi_payload["chat_moderate_enabled"] = chat_moderate_enabled

            # Simple Console Log - Filter out noise
            if "other" not in sammi_payload["trigger"].lower():
                print(f"[{parser_name}] {sammi_payload['trigger']}")
            
            # Send everything to SAMMI (legitimate events and parsed others)
            if allow_sammi:
                send_to_sammi(sammi_payload)

            # --- TTS CHECK ---
            tts_config = config_state.get("tts", {})
            print(f"[DEBUG-TTS-QUEUE] Checking msg from {username}. Global TTS enabled: {tts_config.get('enabled')}, Chat TTS: {chat_tts_enabled}, HAS_TTS: {HAS_TTS}, ST_Allow: {allow_tts}, msg_text: '{msg_text}'")
            if tts_config.get("enabled") and allow_tts and chat_tts_enabled and HAS_TTS and msg_text:
                should_speak = True
                
                # 1. Ignore Commands
                if tts_config.get("ignore_commands", True) and msg_text.startswith("!"):
                    should_speak = False
                    
                # 2. Only Clean Messages
                if tts_config.get("only_clean", True) and is_suspicious:
                    should_speak = False

                # 3. Ignore Users
                ignored_users = [u.lower() for u in tts_config.get("ignored_users", [])]
                if username.lower() in ignored_users:
                    should_speak = False
                    
                # 4. Role Filters
                user_traits = []
                
                # Check for mod/broadcaster
                if data.get("mod"): 
                    user_traits.append("mod")
                    
                # Check for vip
                if data.get("vip"): 
                    user_traits.append("vip")
                    
                # Check for sub
                if data.get("subscriber"): 
                    user_traits.append("sub")
                
                # If they have no special role, they are a non-subscriber
                if not user_traits:
                    user_traits.append("non_sub")

                # Check if ANY of the user's traits are allowed by the configuration
                role_allowed = False
                if "mod" in user_traits and tts_config.get("allow_mods", True): role_allowed = True
                elif "vip" in user_traits and tts_config.get("allow_vips", True): role_allowed = True
                elif "sub" in user_traits and tts_config.get("allow_subs", True): role_allowed = True
                elif "non_sub" in user_traits and tts_config.get("allow_non_subs", True): role_allowed = True
                
                if not role_allowed:
                    should_speak = False
                        
                # 4. Max Length
                max_len = tts_config.get("max_length", 200)
                if max_len > 0 and len(msg_text) > max_len:
                    should_speak = False

                if should_speak:
                    speak_text = msg_text
                    
                    # 5. Ignore URLs
                    if tts_config.get("ignore_urls", True):
                        speak_text = re.sub(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', '', speak_text)
                        
                    # 6. Ignore Emotes (Remove [emote:id] tags if present, or rely on parser)
                    if tts_config.get("ignore_emotes", True):
                        # Remove standard twitch/kick/youtube emote tags 
                        speak_text = re.sub(r'\[(twitch_emote|yt_emoji|emote):.*?\]', '', speak_text, flags=re.IGNORECASE)
                        # Also remove multiple spaces left behind
                        speak_text = re.sub(r'\s+', ' ', speak_text)
                        
                    speak_text = speak_text.strip()
                    
                    if speak_text: # Only speak if there's text left after filtering
                        if tts_config.get("read_username"):
                            speak_text = f"{username} says {speak_text}"
                        
                        vol = tts_config.get("volume", 1.0)
                        rate = tts_config.get("rate", 200)
                        voice_id = tts_config.get("voice_id", "")
                        device_id = tts_config.get("device_id", "")
                        print(f"[DEBUG-TTS-QUEUE] Adding to queue: '{speak_text}'")
                        tts_queue.put((speak_text, vol, rate, voice_id, device_id))
                    elif tts_config.get("play_bell", False):
                        print(f"[DEBUG-TTS-QUEUE] Text empty after filtering, playing bell.")
                        # Text was entirely emotes/urls and got filtered out
                        bell_path = tts_config.get("custom_bell_path", "")
                        tts_queue.put(("[BELL]", tts_config.get("volume", 1.0), 200, bell_path, tts_config.get("device_id", "")))
                elif tts_config.get("play_bell", False):
                    print(f"[DEBUG-TTS-QUEUE] Filtered by rules, playing bell.")
                    # Message was filtered out by rules
                    bell_path = tts_config.get("custom_bell_path", "")
                    tts_queue.put(("[BELL]", tts_config.get("volume", 1.0), 200, bell_path, tts_config.get("device_id", "")))

            # Execute global hotkeys if they match the trigger
            if trigger:
                hotkeys = config_state.get("hotkeys", [])
                for hk in hotkeys:
                    hk_trigger = hk.get("trigger", "").strip()
                    if not hk_trigger:
                        continue
                    
                    # Convert wildcard * to regex .*
                    pattern_str = "^" + re.escape(hk_trigger).replace(r"\*", ".*") + "$"
                    try:
                        if re.match(pattern_str, trigger, re.IGNORECASE):
                            modifiers = []
                            if hk.get("ctrl"): modifiers.append("ctrl")
                            if hk.get("alt"): modifiers.append("alt")
                            if hk.get("shift"): modifiers.append("shift")
                            
                            key = hk.get("key", "").lower()
                            if key:
                                modifiers.append(key)
                                hotkey_str = "+".join(modifiers)
                                print(f"[Hotkeys] Trigger '{trigger}' matched! Simulating keystroke: {hotkey_str}")
                                try:
                                    import keyboard
                                    keys_to_press = hotkey_str.split('+')
                                    for k in keys_to_press: keyboard.press(k)
                                    await asyncio.sleep(0.1) # Hold for 100ms so SAMMI can catch it
                                except Exception as hk_e:
                                    print(f"Failed to send hotkey {hotkey_str}: {hk_e}")
                                finally:
                                    if 'keyboard' in locals() and 'keys_to_press' in locals():
                                        for k in reversed(keys_to_press):
                                            try: keyboard.release(k)
                                            except: pass
                    except Exception as e:
                        print(f"[Hotkeys] Regex error parsing trigger '{hk_trigger}': {e}")

            # Broadcast raw payload for future use
            broadcast({
                "type": "moderation",
                "payload": sammi_payload
            })
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            if not STOP_EVENT.is_set():
                print(f"Main Loop Error: {e}")
            await asyncio.sleep(1)

    print("[Main] Stopping drivers...")
    for t in tasks:
        t.cancel()
    
    # Wait briefly for tasks to cancel
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
        
    await session.close()
    print("[Main] Cleanup complete.")


# ==============================================================================
# 6. TRAY ICON LOGIC
# ==============================================================================

def create_image():
    # Create a simple icon programmatically if no file exists
    from PIL import Image, ImageDraw
    w, h = 64, 64
    image = Image.new('RGB', (w, h), color=(30, 30, 30))
    d = ImageDraw.Draw(image)
    d.rectangle([(16,16), (48,48)], fill=(0, 122, 204))
    d.ellipse([(28,28), (36,36)], fill="white")
    return image

if __name__ == "__main__":
    if HAS_TRAY:
        # Run asyncio loop in a separate thread
        def run_loop():
            # Setup new loop for this thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(main())
            except Exception as e:
                print(f"Loop Error: {e}")
            finally:
                loop.close()

        t = threading.Thread(target=run_loop, daemon=True)
        t.start()
        
        # --- TRAY ICON MENU ---
        def on_exit_click(icon, item):
            icon.stop()
            os._exit(0)
            
        def on_open_settings(icon, item):
            settings_path = os.path.join(BASE_DIR, "settings.html")
            webbrowser.open(f"file://{settings_path}")

        def on_open_chat(icon, item):
            chat_path = os.path.join(BASE_DIR, "chat_overlay.html")

        # --- Dynamic Volume Submenu ---
        def is_vol_checked(item):
            vol_pct = int(item.text.replace('%', ''))
            return abs(GLOBAL_CONFIG.get("tts", {}).get("volume", 1.0) * 100 - vol_pct) < 1

        def set_volume(icon, item):
            vol_pct = int(item.text.replace('%', ''))
            if "tts" not in GLOBAL_CONFIG: GLOBAL_CONFIG["tts"] = {}
            GLOBAL_CONFIG["tts"]["volume"] = vol_pct / 100.0
            # Broadcast the save to file but don't restart script
            try:
                import json
                with open(CONFIG_FILE, "w") as f: json.dump(GLOBAL_CONFIG, f, indent=4)
            except Exception as e:
                print(f"[Tray] Error saving volume: {e}")

        vol_items = []
        # Create elements 100 to 0
        for v in range(100, -1, -10):
            vol_items.append(pystray.MenuItem(f"{v}%", set_volume, radio=True, checked=is_vol_checked))

        # --- Dynamic Output Sound Submenu ---
        def is_dev_checked(item):
            dev = GLOBAL_CONFIG.get("tts", {}).get("device_id", "")
            if item.text == "System Default": return dev == ""
            return dev == item.text

        def set_device(icon, item):
            if "tts" not in GLOBAL_CONFIG: GLOBAL_CONFIG["tts"] = {}
            target = "" if item.text == "System Default" else item.text
            GLOBAL_CONFIG["tts"]["device_id"] = target
            try:
                import json
                with open(CONFIG_FILE, "w") as f: json.dump(GLOBAL_CONFIG, f, indent=4)
            except Exception as e:
                print(f"[Tray] Error saving device: {e}")

        dev_items = [pystray.MenuItem("System Default", set_device, radio=True, checked=is_dev_checked)]
        
        
        for d in _get_audio_devices():
            dev_items.append(pystray.MenuItem(d, set_device, radio=True, checked=is_dev_checked))

        # Create Icon
        image = create_image()
        menu = pystray.Menu(
            pystray.MenuItem("Open Settings", on_open_settings),
            pystray.MenuItem("Open Chat Overlay", on_open_chat),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Output Sound", pystray.Menu(*dev_items)),
            pystray.MenuItem("Volume", pystray.Menu(*vol_items)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", on_exit_click)
        )
        TRAY_ICON = pystray.Icon("StreamerAssistant", image, "StreamerAssistant", menu)
        print("[System] Tray icon started. Check your system tray.")
        TRAY_ICON.run()
        
        # When TRAY_ICON.run() unblocks, see if we need to restart
        if RESTART_PENDING:
            print("[System] Executing postponed restart...")
            import subprocess
            kwargs = {'close_fds': True}
            if os.name == 'nt':
                kwargs['creationflags'] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            subprocess.Popen([sys.executable] + sys.argv[1:], **kwargs)
            os._exit(0)
            
    else:
        # Fallback
        print("[WARN] System Tray dependencies missing (pystray/Pillow). Running in terminal mode.")
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            STOP_EVENT.set()
            print("\nExiting...")






