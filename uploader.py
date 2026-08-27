import os
import sys
import json
import re
import time
import math
import random
import shutil
import glob
import subprocess
import threading
import urllib.parse
import fnmatch
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import cloudscraper
import bencodepy

# --- CONFIGURATION ---
MAX_RETRIES = 5
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
]

def get_random_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "*/*",
        "Connection": "keep-alive"
    }

# --- SMART AUTO-NAMING ---
def get_auto_filename(url):
    default_name = "downloaded_video.mp4"
    if url.startswith("magnet:?"):
        match = re.search(r'dn=([^&]+)', url)
        name = urllib.parse.unquote(match.group(1)) if match else default_name
    else:
        parsed = urllib.parse.urlparse(url)
        path = urllib.parse.unquote(parsed.path)
        name = os.path.basename(path)
        
    name = re.sub(r'[<>:"/\\|?*]', '_', name).strip('. ')
    return name if name and '.' in name else default_name

# --- TORRENT METADATA & SELECTION ---
def parse_torrent_metadata(torrent_path):
    with open(torrent_path, 'rb') as f:
        metadata = bencodepy.decode(f.read())
    
    info = metadata.get(b'info', {})
    file_list = []
    
    if b'files' in info:
        for idx, file in enumerate(info[b'files']):
            path_parts = file[b'path']
            filename = b'/'.join(path_parts).decode('utf-8', errors='ignore')
            size = file[b'length']
            file_list.append({'index': idx + 1, 'name': filename, 'size': size})
    else:
        filename = info.get(b'name', b'unknown').decode('utf-8', errors='ignore')
        size = info.get(b'length', 0)
        file_list.append({'index': 1, 'name': filename, 'size': size})
        
    return file_list

def select_files(file_list, selection_str):
    if not selection_str:
        largest = max(file_list, key=lambda x: x['size'])
        print(f"::notice::No file selection provided. Auto-selecting largest file: {largest['name']}")
        return [largest]
    
    if re.match(r'^[\d,\s]+$', selection_str):
        indices = [int(i.strip()) for i in selection_str.split(',') if i.strip()]
        selected = [f for f in file_list if f['index'] in indices]
        if not selected: raise ValueError(f"No files found for indices: {indices}")
        return selected
    
    selected = [f for f in file_list if fnmatch.fnmatch(f['name'], selection_str)]
    if not selected: raise ValueError(f"No files matched pattern: {selection_str}")
    return selected

def download_magnet_with_selection(magnet_url, dest_name, selection_str):
    meta_dir = "temp_meta"
    download_dir = "temp_download"
    os.makedirs(meta_dir, exist_ok=True)
    os.makedirs(download_dir, exist_ok=True)
    
    try:
        print("::group::Step 1: Downloading torrent metadata")
        cmd_meta = ["aria2c", "--bt-metadata-only=true", "--bt-save-metadata=true", "-d", meta_dir, magnet_url]
        res = subprocess.run(cmd_meta, capture_output=True, text=True)
        if res.returncode != 0: raise Exception(f"Metadata download failed: {res.stderr}")
        
        torrent_files = glob.glob(os.path.join(meta_dir, "*.torrent"))
        if not torrent_files: raise Exception("No .torrent file generated.")
        torrent_path = torrent_files[0]
        print("::endgroup::")

        print("::group::Step 2: Parsing torrent and selecting files")
        file_list = parse_torrent_metadata(torrent_path)
        print(f"Found {len(file_list)} files in torrent.")
        for f in file_list[:5]: print(f"  [{f['index']}] {f['name']} ({f['size']/(1024*1024):.1f} MB)")
        if len(file_list) > 5: print(f"  ... and {len(file_list) - 5} more.")
        
        selected_files = select_files(file_list, selection_str)
        selected_indices = [str(f['index']) for f in selected_files]
        print(f"Selected: {', '.join(f['name'] for f in selected_files)}")
        print("::endgroup::")

        print("::group::Step 3: Downloading selected files via Aria2c")
        cmd_dl = [
            "aria2c", "--seed-time=0", "-d", download_dir,
            "--max-connection-per-server=16", "--split=16", "--min-split-size=1M", 
            "--file-allocation=none", "--continue=true", "--console-log-level=warn", 
            "--summary-interval=1", f"--select-file={','.join(selected_indices)}", torrent_path
        ]
        res = subprocess.run(cmd_dl)
        if res.returncode != 0: raise Exception(f"Aria2 failed with code {res.returncode}")
        print("::endgroup::")

        downloaded_files = [f for f in glob.glob(os.path.join(download_dir, "**/*"), recursive=True) if os.path.isfile(f)]
        if not downloaded_files: raise Exception("No files found after download.")
        
        actual_file = max(downloaded_files, key=os.path.getsize)
        os.rename(actual_file, dest_name)
        
    finally:
        if os.path.exists(meta_dir): shutil.rmtree(meta_dir)
        if os.path.exists(download_dir): shutil.rmtree(download_dir)

# --- LIVE PROGRESS TRACKER ---
class ProgressTracker:
    def __init__(self, total_size):
        self.total_size = total_size
        self.downloaded = 0
        self.lock = threading.Lock()
        self.start_time = time.time()
        self.stop_event = threading.Event()

    def add_bytes(self, bytes_amount):
        with self.lock: self.downloaded += bytes_amount

    def print_progress(self):
        while not self.stop_event.is_set():
            with self.lock: current = self.downloaded
            elapsed = time.time() - self.start_time
            speed = current / elapsed if elapsed > 0 else 0
            
            if self.total_size > 0:
                percent = (current / self.total_size) * 100
                filled = int(30 * percent / 100)
                bar = '█' * filled + '-' * (30 - filled)
                line = f'\r[{bar}] {percent:5.1f}% | {current/(1024*1024):.1f}/{self.total_size/(1024*1024):.1f} MB | {speed/(1024*1024):.1f} MB/s'
            else:
                line = f'\rDownloaded: {current/(1024*1024):.1f} MB | Speed: {speed/(1024*1024):.1f} MB/s'
                
            sys.stdout.write(line)
            sys.stdout.flush()
            time.sleep(1)
        sys.stdout.write('\n')

# --- DISCORD NOTIFICATION ---
def send_to_discord(success, file_name, file_size_mb, page_url, direct_url, download_speed, method, error_msg=""):
    webhook_url = os.environ.get('DISCORD_WEBHOOK_URL')
    if not webhook_url: return

    embed = {
        "title": f"Ultimate Uploader: {'✅ Success' if success else '❌ Failed'}",
        "color": 0x00ff00 if success else 0xff0000,
        "fields": [
            {"name": "📄 File Name", "value": f"`{file_name}`", "inline": True},
            {"name": "📦 Size", "value": f"{file_size_mb:.2f} MB", "inline": True},
            {"name": "⚡ Speed", "value": f"{download_speed:.2f} MB/s", "inline": True},
            {"name": "🛠️ Method", "value": method, "inline": True},
            {"name": "🌐 Watch Page", "value": f"[Click Here]({page_url})" if page_url else "N/A", "inline": False},
            {"name": "🔗 Direct Stream", "value": f"[Direct MP4]({direct_url})" if direct_url else "Could not extract", "inline": False}
        ],
        "footer": {"text": "GitHub Actions Ultimate Engine"},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    }

    if error_msg:
        embed["fields"].append({"name": "⚠️ Error", "value": f"```\n{error_msg[:900]}\n```", "inline": False})

    try:
        requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)
    except Exception as e:
        print(f"::warning::Discord webhook failed: {e}")

# --- 1DM MULTI-THREADED HTTP DOWNLOADER ---
def download_chunk(url, headers, start_byte, end_byte, chunk_path, tracker):
    chunk_headers = headers.copy()
    chunk_headers['Range'] = f'bytes={start_byte}-{end_byte}'
    for attempt in range(MAX_RETRIES):
        try:
            with requests.get(url, headers=chunk_headers, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(chunk_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=1024*1024):
                        if chunk:
                            f.write(chunk)
                            tracker.add_bytes(len(chunk))
            return
        except Exception as e:
            if attempt == MAX_RETRIES - 1: raise Exception(f"Chunk failed: {e}")
            time.sleep(2 ** attempt)

def fast_http_download(url, dest_path, num_threads=8):
    headers = get_random_headers()
    head_resp = requests.head(url, headers=headers, allow_redirects=True, timeout=30)
    supports_ranges = head_resp.headers.get('Accept-Ranges') == 'bytes'
    file_size = int(head_resp.headers.get('Content-Length', 0))

    if not supports_ranges or file_size == 0:
        print("Falling back to standard single-thread stream.")
        with requests.get(url, headers=headers, stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(dest_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=1024*1024):
                    if chunk: f.write(chunk)
        return

    tracker = ProgressTracker(file_size)
    progress_thread = threading.Thread(target=tracker.print_progress)
    progress_thread.start()

    chunk_size = math.ceil(file_size / num_threads)
    temp_dir = "temp_chunks"
    os.makedirs(temp_dir, exist_ok=True)
    tasks = []
    
    for i in range(num_threads):
        start = i * chunk_size
        end = min(start + chunk_size - 1, file_size - 1)
        if start > end: break
        tasks.append((url, headers, start, end, os.path.join(temp_dir, f"chunk_{i:03d}"), tracker))

    try:
        with ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(download_chunk, *task) for task in tasks]
            for future in as_completed(futures): future.result()
    finally:
        tracker.stop_event.set()
        progress_thread.join()

    with open(dest_path, 'wb') as outfile:
        for i in range(len(tasks)):
            with open(os.path.join(temp_dir, f"chunk_{i:03d}"), 'rb') as infile:
                shutil.copyfileobj(infile, outfile)
    shutil.rmtree(temp_dir)

# --- DIRECT LINK EXTRACTION ---
def extract_direct_link(page_url, file_code):
    embed_url = f"https://vidmoly.net/embed-{file_code}.html" if file_code else page_url
    scraper = cloudscraper.create_scraper()
    try:
        r = scraper.get(embed_url, headers={"Referer": "https://vidmoly.net/"}, timeout=30)
        patterns = [
            r'file:\s*["\']([^"\']+\.mp4[^"\']*)["\']',
            r'<source[^>]+src=["\']([^"\']+\.mp4[^"\']*)["\']',
            r'(https?://[^\s"\'<>]+?\.mp4[^\s"\'<>]*)'
        ]
        for pattern in patterns:
            match = re.search(pattern, r.text)
            if match: return match.group(1).split('"')[0].split("'")[0]
    except Exception as e:
        print(f"::warning::Direct link extraction error: {e}")
    return None

# --- MAIN EXECUTION ---
def main():
    video_url = os.environ.get('VIDEO_URL')
    custom_name = os.environ.get('FILE_NAME', '').strip()
    file_selection = os.environ.get('FILE_SELECTION', '').strip()
    api_key = os.environ.get('VIDMOLY_API_KEY')
    num_threads = int(os.environ.get('THREADS', 8))

    if not all([video_url, api_key]):
        print("::error::Missing environment variables.")
        sys.exit(1)

    file_name = custom_name if custom_name else get_auto_filename(video_url)
    if os.path.exists(file_name):
        base, ext = os.path.splitext(file_name)
        file_name = f"{base}_{int(time.time())}{ext}"

    success = False
    page_url = direct_url = file_code = error_msg = ""
    file_size_mb = download_speed = 0.0
    download_method = "Unknown"
    start_time = time.time()

    print(f"::group::Configuration")
    print(f"Target File: {file_name}")
    print(f"URL: {video_url}")
    if video_url.startswith("magnet:?"): print(f"Selection: {file_selection or 'Auto (Largest)'}")
    print("::endgroup::")

    try:
        if video_url.startswith("magnet:?"):
            download_method = "Aria2c (Torrent Select)"
            download_magnet_with_selection(video_url, file_name, file_selection)
        else:
            download_method = f"1DM Engine ({num_threads} Threads)"
            print("::group::Downloading via 1DM Multi-Threaded Engine")
            fast_http_download(video_url, file_name, num_threads)
            print("::endgroup::")
        
        file_size_bytes = os.path.getsize(file_name)
        file_size_mb = file_size_bytes / (1024 * 1024)
        duration = time.time() - start_time
        download_speed = file_size_mb / duration if duration > 0 else 0
        print(f"::notice::Download complete. Size: {file_size_mb:.2f} MB | Speed: {download_speed:.2f} MB/s")
        
    except Exception as e:
        error_msg = f"Download failed: {str(e)}"
        print(f"::error::{error_msg}")
        send_to_discord(False, file_name, file_size_mb, "", "", download_speed, download_method, error_msg)
        sys.exit(1)

    print("::group::Uploading to VidMoly")
    try:
        with open(file_name, 'rb') as f:
            response = requests.post("https://vidmoly.net/api/upload/file", 
                                     data={'api_key': api_key}, 
                                     files={'file': (file_name, f, 'video/mp4')}, timeout=600)
        response.raise_for_status()
        res_data = response.json()
        
        page_url = res_data.get('url') or res_data.get('result', {}).get('url')
        file_code = res_data.get('file_code') or res_data.get('result', {}).get('filecode')
        if not page_url: raise ValueError("No page URL in API response.")
        success = True
        print(f"Upload successful! Page: {page_url}")
    except Exception as e:
        error_msg = f"Upload failed: {str(e)}"
        print(f"::error::{error_msg}")
    print("::endgroup::")

    if success:
        direct_url = extract_direct_link(page_url, file_code) or page_url 

    send_to_discord(success, file_name, file_size_mb, page_url, direct_url, download_speed, download_method, error_msg)

    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary_file:
        summary_file.write(f"### {'✅ Success' if success else '❌ Failed'}\n")
        summary_file.write(f"**File:** `{file_name}` ({file_size_mb:.2f} MB) | **Speed:** {download_speed:.2f} MB/s\n")
        if page_url: summary_file.write(f"**Page:** {page_url}\n")
        if direct_url and direct_url != page_url: summary_file.write(f"**Direct:** {direct_url}\n")

    if not success: sys.exit(1)

if __name__ == "__main__":
    main()
