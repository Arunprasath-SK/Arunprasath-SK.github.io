import os
import subprocess
import json
import time
import glob
import re

# ==========================================
# CONFIGURATION
# ==========================================
INPUT_PATH = "I" # Can be a single file or a folder path
OUTPUT_FOLDER = "output"

# 🎯 Target video stream size in Gigabytes (GiB)
TARGET_VIDEO_GB = 20  # Updated to strictly 20GB

# Temporary Files
RPU_FILE = "RPU.bin"
HDR10PLUS_JSON = "metadata.json"
TEMP_VIDEO = "temp_video.hevc"
TEMP_HDR10PLUS = "temp_hdr10plus.hevc"
FINAL_INJECTED = "final_dual_injected.hevc"

os.makedirs(OUTPUT_FOLDER, exist_ok=True)

# ==========================================
# PART 1: ENVIRONMENT SETUP
# ==========================================
print("--- Checking and Installing Required Tools ---")
setup_cmds = [
    "wget -q -nc https://github.com/quietvoid/dovi_tool/releases/download/2.1.2/dovi_tool-2.1.2-x86_64-unknown-linux-musl.tar.gz",
    "tar -xf dovi_tool-2.1.2-x86_64-unknown-linux-musl.tar.gz && mv dovi_tool-*/dovi_tool ./dovi_tool 2>/dev/null; chmod +x dovi_tool",
    "wget -q -nc https://github.com/quietvoid/hdr10plus_tool/releases/download/1.6.0/hdr10plus_tool-1.6.0-x86_64-unknown-linux-musl.tar.gz",
    "tar -xf hdr10plus_tool-1.6.0-x86_64-unknown-linux-musl.tar.gz && mv hdr10plus_tool-*/hdr10plus_tool ./hdr10plus_tool 2>/dev/null; chmod +x hdr10plus_tool",
    "sudo apt-get update -qq && sudo apt-get install -y -qq mkvtoolnix mediainfo ffmpeg",
    "wget -q -O nvencc.deb https://github.com/rigaya/NVEnc/releases/download/7.66/nvencc_7.66_Ubuntu22.04_amd64.deb",
    "sudo apt-get install -y -qq ./nvencc.deb || sudo apt-get install -f -y -qq"
]

for cmd in setup_cmds:
    subprocess.run(cmd, shell=True)

print("✅ Tools Ready.\n")

# ==========================================
# PART 2: SMART FORMAT SCANNER & AUDIO FILTER
# ==========================================
def verify_format(file_path, label_name):
    print("="*50)
    print(f"🔍 FORMAT VERIFICATION: {label_name}")
    print("="*50)

    if not os.path.exists(file_path):
         print(f"❌ File not found: {file_path}\n")
         return None

    cmd = ['mediainfo', '--Output=JSON', file_path]
    result = subprocess.run(cmd, capture_output=True, text=True)

    try:
        data = json.loads(result.stdout)
        video_track = next((t for t in data.get('media', {}).get('track', []) if t.get('@type') == 'Video'), None)

        if not video_track:
            print("❌ No video track found.\n")
            return None

        hdr_info = str(video_track.get('HDR_Format', '')).upper() + " " + str(video_track.get('HDR_Format_String', '')).upper()
        bit_depth = str(video_track.get('BitDepth', ''))
        transfer_chars = str(video_track.get('transfer_characteristics', '')).upper()

        print(f"▶ 10-bit    : {'✅ DETECTED' if bit_depth == '10' else '❌ Not Found'}")

        has_dv = 'DOLBY VISION' in hdr_info
        has_hdr10plus = 'HDR10+' in hdr_info or 'SMPTE ST 2094' in hdr_info
        has_hdr10 = 'HDR10' in hdr_info or 'SMPTE ST 2086' in hdr_info

        print(f"▶ DV        : {'✅ DETECTED' if has_dv else '❌ Not Found'}")
        print(f"▶ HDR10+    : {'✅ DETECTED' if has_hdr10plus else '❌ Not Found'}")
        print(f"▶ HDR10     : {'✅ DETECTED' if has_hdr10 else '❌ Not Found'}")

        print("="*50 + "\n")
        return {'dv': has_dv, 'hdr10plus': has_hdr10plus, 'hdr10': has_hdr10}

    except Exception as e:
         print(f"Error during verification: {e}\n")
         return None

def get_best_audio_tracks(file_path):
    """Scans and selects Tamil tracks and the single highest quality English track."""
    print("⏳ [ SCANNING ] Analyzing audio stream fidelity...")
    cmd = ['mkvmerge', '-i', file_path, '-F', 'json']
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    try:
        data = json.loads(result.stdout)
        audio_tracks = [t for t in data.get('tracks', []) if t.get('type') == 'audio']
        
        keep_ids = []
        eng_tracks = []
        
        for t in audio_tracks:
            lang = t.get('properties', {}).get('language', 'und').lower()
            track_id = str(t.get('id'))
            channels = t.get('properties', {}).get('audio_channels', 2)
            codec = t.get('properties', {}).get('codec_id', '').upper()
            
            # Score track based on channels and lossless codecs (TrueHD/DTS-HD/FLAC)
            score = int(channels) * 10
            if 'TRUEHD' in codec: score += 5
            elif 'DTS' in codec: score += 5
            elif 'FLAC' in codec: score += 4
            elif 'PCM' in codec: score += 4
            elif 'EAC3' in codec: score += 3
            
            if lang in ['tam', 'ta']:
                keep_ids.append(track_id)
                print(f"  └─ Keep: Tamil Audio (Track {track_id} | {codec} | {channels}ch)")
            elif lang in ['eng', 'en']:
                eng_tracks.append({'id': track_id, 'score': score, 'codec': codec, 'ch': channels})
        
        # Determine the absolute best English track
        if eng_tracks:
            eng_tracks.sort(key=lambda x: x['score'], reverse=True)
            best_eng = eng_tracks[0]
            keep_ids.append(best_eng['id'])
            print(f"  └─ Keep: High-Res English (Track {best_eng['id']} | {best_eng['codec']} | {best_eng['ch']}ch)")
            
        if not keep_ids and audio_tracks:
            fallback_id = str(audio_tracks[0]['id'])
            keep_ids.append(fallback_id)
            print(f"⚠️ [ WARNING ] No Tamil/English found. Falling back to Track {fallback_id}.")
            
        return ",".join(keep_ids)
        
    except Exception as e:
        print(f"⚠️ [ WARNING ] Audio track parsing failed: {e}. Defaulting to standard language codes.")
        return "tam,eng"

# ==========================================
# PART 3: ADAPTIVE PIPELINE METADATA
# ==========================================
def get_video_fps(file_path):
    try:
        cmd = [
            'ffprobe', '-v', 'error', 
            '-select_streams', 'v:0', 
            '-show_entries', 'stream=r_frame_rate', 
            '-of', 'default=noprint_wrappers=1:nokey=1', 
            file_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        fps_fraction = result.stdout.strip()
        if fps_fraction: return f"{fps_fraction}fps"
    except Exception: pass
    return "24000/1001fps"

def get_video_duration(file_path):
    try:
        cmd = [
            'ffprobe', '-v', 'error', 
            '-select_streams', 'v:0', 
            '-show_entries', 'stream=duration', 
            '-of', 'default=noprint_wrappers=1:nokey=1', 
            file_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        duration = result.stdout.strip()
        if duration and duration != "N/A": return float(duration)
            
        cmd = [
            'ffprobe', '-v', 'error', 
            '-show_entries', 'format=duration', 
            '-of', 'default=noprint_wrappers=1:nokey=1', 
            file_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        return float(result.stdout.strip())
    except Exception:
        return None

def clean_number_string(s):
    s = str(s).replace("cd/m2", "").replace("cd/m²", "").strip()
    return re.findall(r"[\d\.]+", s)

def get_hdr10_metadata(file_path):
    max_cll_val, max_fall_val = None, None
    min_l_val, max_l_val = None, None
    
    gx, gy = "13250", "34500"
    bx, by = "7500", "3000"
    rx, ry = "34000", "16000"
    wpx, wpy = "15635", "16450"
    cc_mkv = "0.68000,0.32000,0.26500,0.69000,0.15000,0.06000"
    wc_mkv = "0.31270,0.32900"

    try:
        cmd = ['mediainfo', '--Output=JSON', file_path]
        result = subprocess.run(cmd, capture_output=True, text=True)
        data = json.loads(result.stdout)
        track = next((t for t in data.get('media', {}).get('track', []) if t.get('@type') == 'Video'), {})

        cll_str, fall_str = track.get('MaxCLL'), track.get('MaxFALL')
        lum_str = track.get('MasteringDisplay_Luminance')
        cc_str = track.get('MasteringDisplay_ChromaticityCoordinates')

        if cll_str:
            matches = clean_number_string(cll_str)
            if matches: max_cll_val = matches[0]
            
        if fall_str:
            matches = clean_number_string(fall_str)
            if matches: max_fall_val = matches[0]

        if lum_str:
            lum_matches = clean_number_string(lum_str)
            if len(lum_matches) >= 2:
                min_l_val, max_l_val = lum_matches[0], lum_matches[1]
                if float(min_l_val) > float(max_l_val):
                    min_l_val, max_l_val = max_l_val, min_l_val

        if cc_str:
            cc_parts = [p.strip() for p in str(cc_str).split(',')]
            if len(cc_parts) == 8:
                rx, ry, gx, gy, bx, by, wpx, wpy = cc_parts
                cc_mkv = f"{rx},{ry},{gx},{gy},{bx},{by}"
                wc_mkv = f"{wpx},{wpy}"

    except Exception: pass

    nvencc_args, mkv_args_str = "", ""

    if max_l_val is not None and min_l_val is not None:
        try:
            nvencc_max_l, nvencc_min_l = int(float(max_l_val) * 10000), int(float(min_l_val) * 10000)
        except:
            nvencc_max_l, nvencc_min_l = 10000000, 1
            
        master_display = f"G({gx},{gy})B({bx},{by})R({rx},{ry})WP({wpx},{wpy})L({nvencc_max_l},{nvencc_min_l})"
        nvencc_args += f'--master-display "{master_display}" '
        mkv_args_str += f"--chromaticity-coordinates 0:{cc_mkv} --white-colour-coordinates 0:{wc_mkv} --max-luminance 0:{max_l_val} --min-luminance 0:{min_l_val} "

    if max_cll_val is not None and max_fall_val is not None:
        nvencc_args += f'--max-cll "{max_cll_val},{max_fall_val}" '
        mkv_args_str += f"--max-content-light 0:{max_cll_val} --max-frame-light 0:{max_fall_val} "

    return nvencc_args.strip(), mkv_args_str.strip()

# ==========================================
# PART 4: THE PIPELINE ENGINE
# ==========================================
def run_cmd_silent(cmd, step_name):
    print(f"⏳ [ RUNNING ] {step_name}...", end="", flush=True)
    start_time = time.time()
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    elapsed = time.time() - start_time
    if result.returncode != 0:
        error_msg = result.stderr.strip() if result.stderr.strip() else result.stdout.strip()
        print(f"\r❌ [ ERROR ] {step_name} failed!\n")
        print(f"--- ERROR LOG ---\n{error_msg[-1500:]}\n-----------------\n")
        return False
    print(f"\r✅ [ DONE ] {step_name} (Took {elapsed/60:.2f} minutes)")
    return True

def process_video(input_path, output_path, bitrate_kbps, duration_secs):
    print(f"\n🎬 PROCESSING: {os.path.basename(input_path)}")
    total_start = time.time()

    flags = verify_format(input_path, "SOURCE FILE")
    if flags is None: return False

    has_dv = flags['dv']
    has_hdr10plus = flags['hdr10plus']
    source_fps = get_video_fps(input_path)
    
    print(f"▶ Detected Frame Rate : {source_fps}")
    print(f"▶ Target Video Size   : {TARGET_VIDEO_GB} GB")
    print(f"▶ Encoding Bitrate    : {bitrate_kbps} kbps\n")

    audio_track_ids = get_best_audio_tracks(input_path)

    if has_dv:
        if not run_cmd_silent(f'ffmpeg -y -i "{input_path}" -c:v copy -vbsf hevc_mp4toannexb -f hevc - | ./dovi_tool extract-rpu - -o "{RPU_FILE}"', "Extracting Dolby Vision RPU"): return False
    if has_hdr10plus:
        if not run_cmd_silent(f'ffmpeg -y -i "{input_path}" -c:v copy -vbsf hevc_mp4toannexb -f hevc - | ./hdr10plus_tool extract - -o "{HDR10PLUS_JSON}"', "Extracting HDR10+ JSON"): return False

    nvencc_hdr_args, mkvmerge_hdr_args = get_hdr10_metadata(input_path)

    encode_cmd = (f'nvencc --avhw -i "{input_path}" -o "{TEMP_VIDEO}" '
                  f'-c hevc --profile main10 --tier high --output-depth 10 '
                  f'--preset P7 --cbr {bitrate_kbps} '
                  f'--colorprim bt2020 --transfer smpte2084 --colormatrix bt2020nc '
                  f'{nvencc_hdr_args} --aud')

    if not run_cmd_silent(encode_cmd, "NVEncC GPU Encode"): return False

    current_video = TEMP_VIDEO

    if has_hdr10plus:
        if not run_cmd_silent(f'./hdr10plus_tool inject -i "{current_video}" -j "{HDR10PLUS_JSON}" -o "{TEMP_HDR10PLUS}"', "Injecting HDR10+ Metadata"): return False
        current_video = TEMP_HDR10PLUS

    if has_dv:
        if not run_cmd_silent(f'./dovi_tool inject-rpu -i "{current_video}" --rpu-in "{RPU_FILE}" -o "{FINAL_INJECTED}"', "Injecting Dolby Vision RPU"): return False
        current_video = FINAL_INJECTED

    print("⏳ [ WAITING ] Letting Drive I/O catch up...", end="", flush=True)
    time.sleep(5)
    print("\r✅ [ DONE ] Letting Drive I/O catch up...      ")

    # Final Mux: Adds the new video, drops the old video, strips all subs/attachments, and keeps only targeted audio
    mkvmerge_cmd = (f'mkvmerge -o "{output_path}" --default-duration 0:{source_fps} '
                    f'{mkvmerge_hdr_args} '
                    f'--fix-bitstream-timing-information 0 "{current_video}" '
                    f'--no-video --audio-tracks {audio_track_ids} --no-subtitles --no-attachments --no-track-tags "{input_path}"')

    if not run_cmd_silent(mkvmerge_cmd, "Final MKVMerge Stream Filtering & Muxing"): return False

    for f in [RPU_FILE, HDR10PLUS_JSON, TEMP_VIDEO, TEMP_HDR10PLUS, FINAL_INJECTED]:
        if os.path.exists(f): os.remove(f)

    total_elapsed = (time.time() - total_start) / 60
    print(f"\n🎉 Finished '{os.path.basename(input_path)}' cleanly in {total_elapsed:.2f} minutes!\n")

    verify_format(output_path, "FINAL OUTPUT FILE")
    return True

# ==========================================
# EXECUTE SCRIPT (BATCH PRE-SCAN & AUTO-START)
# ==========================================
mkv_files = []

if os.path.isfile(INPUT_PATH):
    if INPUT_PATH.lower().endswith('.mkv'): mkv_files.append(INPUT_PATH)
    else: print(f"❌ Error: The file '{INPUT_PATH}' is not an MKV file.")
elif os.path.isdir(INPUT_PATH):
    mkv_files = glob.glob(os.path.join(INPUT_PATH, "*.mkv"))
else:
    print(f"❌ Error: The path '{INPUT_PATH}' does not exist.")

if not mkv_files:
    print(f"❌ No MKV files found to process in {INPUT_PATH}.")
else:
    print(f"📦 Found {len(mkv_files)} file(s) to scan.\n")
    print("="*50 + "\n📊 BATCH PRE-SCAN SUMMARY\n" + "="*50)
    
    files_to_process = []
    
    for file_path in mkv_files:
        file_name = os.path.basename(file_path)
        out_path = os.path.join(OUTPUT_FOLDER, f"Processed_{file_name}")

        if os.path.exists(out_path): continue

        duration_secs = get_video_duration(file_path)
        if not duration_secs: continue

        target_bytes = TARGET_VIDEO_GB * 1024 * 1024 * 1024 
        target_bits = target_bytes * 8
        bitrate_kbps = int((target_bits / duration_secs) / 1000)
        
        files_to_process.append({
            'input_path': file_path, 'output_path': out_path,
            'bitrate_kbps': bitrate_kbps, 'duration_secs': duration_secs
        })
        
        print(f"▶ {file_name}\n  └─ Duration: {duration_secs:.2f}s | Target: {bitrate_kbps} kbps")

    print("="*50)

    if files_to_process:
        print(f"\n✅ Auto-confirmed {len(files_to_process)} file(s). Starting background processing...\n")
        for file_data in files_to_process:
            process_video(file_data['input_path'], file_data['output_path'], file_data['bitrate_kbps'], file_data['duration_secs'])
        print("✅ All queued files have been processed!")
        