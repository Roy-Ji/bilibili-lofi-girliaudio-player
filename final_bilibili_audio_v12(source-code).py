#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bilibili 直播音频提取器 (v12 正式稳定版)
- 基于 v12 Debug 版优化
- 移除终端啰嗦输出，全部写入 Log 文件
- 保持 ADTS 格式兼容 PotPlayer
"""

import os
import sys
import time
import signal
import subprocess
import threading
import logging
import logging.handlers
from http.server import HTTPServer, BaseHTTPRequestHandler

# ================== 配置区 ==================
FFMPEG_PATH = r"D:\FFmpeg\bin\ffmpeg.EXE"
POTPLAYER_PATH = r"C:\Program Files\DAUM\PotPlayer\PotPlayerMini64.exe"

ROOM_ID = "27519423"
HTTP_PORT = 8765
AUDIO_URL = f"http://127.0.0.1:{HTTP_PORT}/audio.aac"

# 优化参数
PRELOAD_TIME = 2

ffmpeg_proc = None
streamlink_proc = None
potplayer_proc = None
httpd = None
shutdown_event = threading.Event()
audio_buffer = bytearray()
buffer_lock = threading.Lock()

# ================== 1. 配置日志系统 (写入文件) ==================
def setup_logger():
    # 获取当前脚本所在目录
    current_script_path = os.path.abspath(__file__)
    program_dir = os.path.dirname(current_script_path)
    # 构建项目根目录下的 log 文件夹路径
    bilibili_audio_dir = os.path.dirname(program_dir)
    log_dir = os.path.join(bilibili_audio_dir, 'log')
    os.makedirs(log_dir, exist_ok=True) # 确保 log 文件夹存在

    # 生成带时间戳的日志文件名
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    log_filename = os.path.join(log_dir, f'bilibili_audio_v12_final_{timestamp}.log')

    # 创建 logger
    logger = logging.getLogger()
    logger.setLevel(logging.INFO) # 默认只记录 INFO 及以上级别

    # 1. File Handler (记录所有 DEBUG 信息到文件)
    file_handler = logging.handlers.RotatingFileHandler(
        log_filename, 
        maxBytes=10*1024*1024, # 10MB
        backupCount=2, 
        encoding='utf-8'
    )
    file_formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
    file_handler.setFormatter(file_formatter)
    file_handler.setLevel(logging.DEBUG) # 文件里记录详细点

    # 2. Console Handler (只在终端显示关键信息)
    console_handler = logging.StreamHandler()
    console_formatter = logging.Formatter('[%(levelname)s] %(message)s')
    console_handler.setFormatter(console_formatter)
    console_handler.setLevel(logging.INFO) # 终端只看关键信息

    # 添加 handlers
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger

# 初始化日志
log = setup_logger()

# ================== HTTP 服务器 (精简版) ==================
class AudioStreamHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # 禁用 BaseHTTPServer 的默认日志，全部交给我们的 logging 处理
        pass

    def do_GET(self):
        log.info(f"📥 新请求: {self.path} from {self.client_address}")
        
        # 简单的路径检查
        if self.path not in ["/", "/audio.aac"]:
            log.warning(f"❌ 路径错误: {self.path}")
            self.send_error(404)
            return

        try:
            # --- 响应头 ---
            self.send_response(200)
            self.send_header('Content-Type', 'audio/aac')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Accept-Ranges', 'none')
            self.end_headers()
            log.info("📤 HTTP 200 响应已发送")

            # --- 预加载数据 ---
            preload_data = b""
            with buffer_lock:
                if audio_buffer:
                    preload_data = bytes(audio_buffer)
            
            if preload_data:
                self.wfile.write(preload_data)
                self.wfile.flush()
                log.info(f"✅ 发送预加载数据: {len(preload_data)} 字节")
            else:
                log.warning("⚠️ 无预加载数据")

            # --- 流式传输 ---
            log.info("🔄 开始流式传输...")
            while not shutdown_event.is_set():
                if ffmpeg_proc is None or ffmpeg_proc.poll() is not None:
                    break

                try:
                    data = ffmpeg_proc.stdout.read(4096)
                    if data:
                        self.wfile.write(data)
                        self.wfile.flush()
                    else:
                        time.sleep(0.01)
                except (ConnectionResetError, BrokenPipeError):
                    log.info("🛑 客户端断开连接")
                    break
                except Exception as e:
                    log.error(f"⚡ 传输异常: {e}")
                    break

        except Exception as e:
            log.error(f"🚨 处理请求失败: {e}")

# ================== 主程序 ==================
def main():
    global ffmpeg_proc, streamlink_proc, potplayer_proc

    log.info(f"🚀 程序启动 | 目标直播间: {ROOM_ID}")
    
    # 检查依赖
    if not os.path.isfile(FFMPEG_PATH):
        log.critical(f"❌ 找不到 ffmpeg: {FFMPEG_PATH}")
        return
    if not os.path.isfile(POTPLAYER_PATH):
        log.critical(f"❌ 找不到 PotPlayer: {POTPLAYER_PATH}")
        return

    # --- 1. 启动管道 ---
    try:
        streamlink_cmd = [
            "streamlink", 
            "--stdout", 
            "--loglevel", "error", 
            f"https://live.bilibili.com/{ROOM_ID}", 
            "best"
        ]
        
        # 保持 v12 验证有效的 adts 格式
        ffmpeg_cmd = [
            FFMPEG_PATH, 
            "-loglevel", "info", # 保留 FFmpeg 的 info 日志以便排查转码问题
            "-i", "pipe:0",
            "-vn", 
            "-c:a", "aac", 
            "-b:a", "128k",
            "-ar", "44100", 
            "-f", "adts", 
            "-"
        ]

        log.info("⚙️ 启动 Streamlink 和 FFmpeg 管道...")
        streamlink_proc = subprocess.Popen(streamlink_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        ffmpeg_proc = subprocess.Popen(ffmpeg_cmd, stdin=streamlink_proc.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        streamlink_proc.stdout.close() # 防止死锁
        
        log.info("✅ 管道启动成功")

    except Exception as e:
        log.critical(f"❌ 管道启动失败: {e}")
        return

    # --- 2. 预加载 ---
    log.info(f"⏳ 预加载 {PRELOAD_TIME} 秒音频...")
    preload_buffer = bytearray()
    start_time = time.time()

    # 开启线程读取 FFmpeg 的 stderr (转码错误/警告会在这里)
    def log_ffmpeg_stderr():
        for line in iter(ffmpeg_proc.stderr.readline, b''):
            if line:
                log.info(f"🎥 FFmpeg: {line.decode('utf-8', errors='replace').strip()}")
    
    ffmpeg_log_thread = threading.Thread(target=log_ffmpeg_stderr, daemon=True)
    ffmpeg_log_thread.start()

    while time.time() - start_time < PRELOAD_TIME:
        if shutdown_event.is_set():
            return
        data = ffmpeg_proc.stdout.read(8192)
        if data:
            preload_buffer.extend(data)
        time.sleep(0.05)
    
    with buffer_lock:
        audio_buffer[:] = preload_buffer
    log.info(f"✅ 预加载完成: {len(audio_buffer)} 字节")

    # --- 3. 启动 HTTP ---
    def run_server():
        global httpd
        try:
            httpd = HTTPServer(('127.0.0.1', HTTP_PORT), AudioStreamHandler)
            log.info(f"🌐 HTTP 服务已启动: http://127.0.0.1:{HTTP_PORT}/")
            httpd.serve_forever()
        except Exception as e:
            log.error(f"❌ HTTP 服务器错误: {e}")

    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
    time.sleep(0.5)

    # --- 4. 启动播放器 ---
    try:
        potplayer_proc = subprocess.Popen([POTPLAYER_PATH, AUDIO_URL])
        log.info("▶️ PotPlayer 已启动")
    except Exception as e:
        log.critical(f"❌ 启动 PotPlayer 失败: {e}")
        return

    # --- 5. 主循环 (监控) ---
    try:
        while not shutdown_event.is_set():
            if potplayer_proc.poll() is not None:
                log.info("⏹️ 检测到 PotPlayer 关闭，准备退出")
                break
            if ffmpeg_proc.poll() is not None:
                log.warning("⏹️ FFmpeg 进程异常退出")
                break
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("🛑 用户中断程序")
    finally:
        shutdown_event.set()
        if httpd:
            httpd.shutdown()
        log.info("👋 程序已安全退出")

if __name__ == "__main__":

    main()
