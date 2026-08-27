import os
import requests
import sys
import json
import re
import time
import subprocess
import glob
import shutil
import math
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
import cloudscraper

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

# --- DISCORD NOTIFICATION ---
def send_to_discord(success, file_name, file_size_mb, page_url, direct_url, download_speed, method, error_msg=""):
    webhook_url = os.environ.get('DISCORD_WEBHOOK_URL')
    if not webhook_url:
        print("Warning: DISCORD_WEBHOOK_URL not set. Skipping Discord notification.")
        return

    color = 0x00ff00 if success else 0xff0000
    status = "✅ Upload Successful" if success else "❌ Upload Failed"

    embed = {
        "title": f"1DM Uploader: {status}",
        "color": color,
        "fields": [
            {"name": "📄 File Name", "value": f"`{file_name}`", "inline": True},
            {"name": "📦 File Size", "value": f"{file_size_mb:.2f} MB", "inline": True},
            {"name": "⚡ Download Speed", "value": f"{download_speed:.2f} MB/s", "inline": True},
            {"name": "🛠️ Download Method", "value": method, "inline": True},
            {"name": "🌐 Watch Page", "value": f"[Click Here]({page_url})" if page_url else "N/A", "inline": False},
            {"name": "🔗 Direct Stream", "value": f"[Direct MP4]({direct_url})" if direct_url else "Could not extract", "inline": False}
        ],
        "footer": {"text": "Powered by GitHub Actions & 1DM Engine"},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    }

    if error_msg:
        clean_error = error_msg[:1000].replace("```", "").replace("`", "") 
        embed["fields"].append({"name": "⚠️ Error Details", "value": f"```\n{clean_error}\n```", "inline": False})

    try:
        req = requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)
        if req.status_code in [200, 204]:
            print("✅ Discord notification sent.")
        else:
            print(f"❌ Discord webhook failed: {req.status_code} - {req.text}")
    except Exception as e:
        print(f"❌ Failed to send Discord notification: {e}")

# --- 1DM STYLE MULTI-THREADED HTTP DOWNLOADER ---
def download_chunk(url, headers, start_byte, end_byte, chunk_path):
    """Downloads a specific byte range with aggressive retry logic."""
    chunk_headers = headers.copy()
    chunk_headers['Range'] = f'bytes={start_byte}-{end_byte}'
    
    for attempt in range(MAX_RETRIES):
        try:
            with requests.get(url, headers=chunk_headers, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(chunk_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=1024*1024): # 1MB chunks
                        if chunk:
                            f.write(chunk)
            return True
        except Exception as e:
            if attempt == MAX_RETRIES - 1:
                raise Exception(f"Chunk failed after {MAX_RETRIES} retries: {e}")
            time.sleep(2 ** attempt) # Exponential backoff

def fast_http_download(url, dest_path, num_threads=8):
    """Orchestrates multi-threaded chunked downloading."""
    headers = get_random_headers()
    print(f"Checking server capabilities for multi-threading...")
    
    try:
        head_resp = requests.head(url, headers=headers, allow_redirects=True, timeout=30)
        supports_ranges = head_resp.headers.get('Accept-Ranges') == 'bytes'
        file_size = int(head_resp.headers.get('Content-Length', 0))
    except Exception as e:
        raise Exception(f"Failed to get file metadata: {e}")

    if not supports_ranges or file_size == 0:
        print("⚠️ Server does not support multi-threading. Falling back to standard stream.")
        return standard_http_download(url, dest_path, headers)

    print(f"✅ Server supports ranges. Downloading {file_size} bytes in {num_threads} threads...")
    
    chunk_size = math.ceil(file_size / num_threads)
    temp_dir = "temp_chunks"
    os.makedirs(temp_dir, exist_ok=True)
    
    tasks = []
    for i in range(num_threads):
        start = i * chunk_size
        end = min(start + chunk_size - 1, file_size - 1)
        if start > end:
            break
        chunk_path = os.path.join(temp_dir, f"chunk_{i:03d}")
        tasks.append((url, headers, start, end, chunk_path))

    # Execute downloads in parallel
    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(download_chunk, *task) for task in tasks]
        for future in as_completed(futures):
            future.result() # Raise exception if a chunk failed

    # Merge chunks into final file
    print("Merging chunks...")
    with open(dest_path, 'wb') as outfile:
        for i in range(len(tasks)):
            chunk_path = os.path.join(temp_dir, f"chunk_{i:03d}")
            with open(chunk_path, 'rb') as infile:
                shutil.copyfileobj(infile, outfile)
    
    shutil.rmtree(temp_dir)
    print("✅ Merge complete.")

def standard_http_download(url, dest_path, headers):
    """Fallback single-threaded downloader."""
    with requests.get(url, headers=headers, stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(dest_path, 'wb') as f:
            for chunk in r.iter_content(chunk_size=1024*1024):
                if chunk:
                    f.write(chunk)

# --- DIRECT LINK EXTRACTION (Cloudflare Bypass) ---
def extract_direct_link(page_url, file_code):
    if not page_url and not file_code:
        return None
    
    embed_url = f"https://vidmoly.net/embed-{file_code}.html" if file_code else page_url
    scraper = cloudscraper.create_scraper()
    
    try:
        print(f"Bypassing Cloudflare to extract direct link from: {embed_url}")
        r = scraper.get(embed_url, headers={"Referer": "https://vidmoly.net/"}, timeout=30)
        
        patterns = [
            r'file:\s*["\']([^"\']+\.mp4[^"\']*)["\']',
            r'<source[^>]+src=["\']([^"\']+\.mp4[^"\']*)["\']',
            r'(https?://[^\s"\'<>]+?\.mp4[^\s"\'<>]*)'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, r.text)
            if match:
                direct = match.group(1).split('"')[0].split("'")[0]
                print(f"✅ Extracted Direct Link: {direct}")
                return direct
                
        print("⚠️ Could not find direct .mp4 link. VidMoly may have updated their player.")
    except Exception as e:
        print(f"⚠️ Error extracting direct link: {e}")
        
    return None

# --- MAIN EXECUTION ---
def main():
    video_url = os.environ.get('VIDEO_URL')
    file_name = os.environ.get('FILE_NAME', 'video.mp4')
    api_key = os.environ.get('VIDMOLY_API_KEY')
    num_threads = int(os.environ.get('THREADS', 8))

    if not all([video_url, api_key]):
        print("Error: Missing environment variables.")
        sys.exit(1)

    success = False
    page_url = direct_url = file_code = error_msg = ""
    file_size_mb = download_speed = 0.0
    download_method = "Unknown"

    # --- 1. DOWNLOAD ---
    print(f"Processing URL: {video_url}")
    start_time = time.time()
    
    try:
        if video_url.startswith("magnet:?"):
            download_method = "Aria2c (Maxed Torrent)"
            print("Detected Magnet link. Downloading via maxed-out aria2c...")
            os.makedirs("temp_download", exist_ok=True)
            
            # Maxed out aria2c flags for maximum torrent speed
            cmd = [
                "aria2c", "--seed-time=0", "-d", "temp_download",
                "--max-connection-per-server=16", "--split=16", 
                "--min-split-size=1M", "--file-allocation=none", "--continue=true",
                video_url
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            if result.returncode != 0:
                raise Exception(f"Aria2 failed: {result.stderr}")
            
            video_files = glob.glob("temp_download/*")
            if not video_files:
                raise Exception("No files found after magnet download.")
            
            actual_file = max(video_files, key=os.path.getsize)
            os.rename(actual_file, file_name)
            shutil.rmtree("temp_download")
        else:
            download_method = f"1DM Engine ({num_threads} Threads)"
            print(f"Downloading via 1DM Multi-Threaded Engine...")
            fast_http_download(video_url, file_name, num_threads)
        
        file_size_bytes = os.path.getsize(file_name)
        file_size_mb = file_size_bytes / (1024 * 1024)
        duration = time.time() - start_time
        download_speed = file_size_mb / duration if duration > 0 else 0
        
        print(f"✅ Download complete. Size: {file_size_mb:.2f} MB | Speed: {download_speed:.2f} MB/s")
        
    except Exception as e:
        error_msg = f"Download failed: {str(e)}"
        print(error_msg)
        send_to_discord(False, file_name, file_size_mb, "", "", download_speed, download_method, error_msg)
        sys.exit(1)

    # --- 2. UPLOAD TO VIDMOLY ---
    print("Uploading to VidMoly...")
    upload_url = "https://vidmoly.net/api/upload/file" 
    data = {'api_key': api_key}

    try:
        with open(file_name, 'rb') as f:
            files = {'file': (file_name, f, 'video/mp4')}
            response = requests.post(upload_url, data=data, files=files, timeout=600)
            
        response.raise_for_status()
        response_data = response.json()
        print("VidMoly API Response:", json.dumps(response_data, indent=2))
        
        page_url = response_data.get('url') or response_data.get('link') or response_data.get('result', {}).get('url')
        file_code = response_data.get('file_code') or response_data.get('result', {}).get('filecode')
        
        if not page_url:
            raise ValueError("Upload succeeded but no page URL was returned.")
            
        success = True
        print(f"✅ Upload successful! Page URL: {page_url}")
        
    except Exception as e:
        error_msg = f"Upload failed: {str(e)}"
        print(error_msg)

    # --- 3. EXTRACT DIRECT LINK ---
    if success:
        direct_url = extract_direct_link(page_url, file_code)
        if not direct_url:
            direct_url = page_url 

    # --- 4. SEND DISCORD NOTIFICATION ---
    send_to_discord(success, file_name, file_size_mb, page_url, direct_url, download_speed, download_method, error_msg)

    # Write to GitHub Actions Summary
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary_file:
        summary_file.write(f"### {'✅ Success' if success else '❌ Failed'}\n")
        summary_file.write(f"**File:** `{file_name}` ({file_size_mb:.2f} MB)\n")
        summary_file.write(f"**Speed:** {download_speed:.2f} MB/s via {download_method}\n")
        if page_url: summary_file.write(f"**Page:** {page_url}\n")
        if direct_url and direct_url != page_url: summary_file.write(f"**Direct:** {direct_url}\n")

    if not success:
        sys.exit(1)

if __name__ == "__main__":
    main()
