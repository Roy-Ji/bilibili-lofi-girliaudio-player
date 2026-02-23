#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bilibili 直播音频提取器 (v13 正式稳定版 - PyInstaller打包优化版)
- 基于 v12 Debug 版优化
- 移除终端啰嗦输出，全部写入 Log 文件
- 保持 ADTS 格式兼容 PotPlayer
- 支持从PATH或同目录查找依赖
- 修复：彻底隐藏子进程黑窗口
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
# 优先从PATH获取，否则使用同目录下的可执行文件
def find_executable(name, default_name):
    """从PATH或程序同目录查找可执行文件"""
    # 1. 尝试从PATH查找
    from shutil import which
    path_exe = which(name)
    if path_exe:
        return path_exe
   
    # 2. 尝试程序同目录（打包后场景）
    if getattr(sys, 'frozen', False):
        # PyInstaller打包后的运行目录
        base_path = sys._MEIPASS
    else:
        # 开发环境：脚本所在目录
        base_path = os.path.dirname(os.path.abspath(__file__))
   
    # 尝试常见命名
    candidates = [
        os.path.join(base_path, f"{name}.exe"),
        os.path.join(base_path, name),
        os.path.join(base_path, "tools", f"{name}.exe"),
        os.path.join(base_path, "tools", name),
    ]
   
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
   
    # 返回默认名称，让系统PATH去解析（如果用户已添加PATH）
    return default_name

FFMPEG_PATH = find_executable("ffmpeg", "ffmpeg")
POTPLAYER_PATH = find_executable("PotPlayerMini64", "PotPlayerMini64")

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

# ================== 1. 配置日志系统 (写入AppData) ==================
def setup_logger():
    # 确定日志目录：AppData\Local\bilibili_audio_player\logs
    if os.name == 'nt':  # Windows
        appdata = os.environ.get('LOCALAPPDATA')
        if not appdata:
            appdata = os.path.expanduser('~\\AppData\\Local')
    else:
        appdata = os.path.expanduser('~/.local/share')
   
    log_dir = os.path.join(appdata, 'bilibili_audio_player', 'logs')
    os.makedirs(log_dir, exist_ok=True)
   
    # 生成带时间戳的日志文件名
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    log_filename = os.path.join(log_dir, f'bilibili_audio_{timestamp}_exeprogram.log')

    # 创建 logger
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    # 1. File Handler (记录所有 DEBUG 信息到文件)
    file_handler = logging.handlers.RotatingFileHandler(
        log_filename,
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    file_formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
    file_handler.setFormatter(file_formatter)
    file_handler.setLevel(logging.DEBUG)

    # 2. Console Handler (只在终端显示关键信息)
    console_handler = logging.StreamHandler()
    console_formatter = logging.Formatter('[%(levelname)s] %(message)s')
    console_handler.setFormatter(console_formatter)
    console_handler.setLevel(logging.INFO)

    # 添加 handlers
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
   
    # 记录日志位置
    logger.info(f"日志文件位置: {log_filename}")
   
    return logger

# 初始化日志
log = setup_logger()

# ================== HTTP 服务器 (精简版) ==================
class AudioStreamHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        log.info(f"📥 新请求: {self.path} from {self.client_address}")
       
        if self.path not in ["/", "/audio.aac"]:
            log.warning(f"❌ 路径错误: {self.path}")
            self.send_error(404)
            return

        try:
            self.send_response(200)
            self.send_header('Content-Type', 'audio/aac')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Accept-Ranges', 'none')
            self.end_headers()
            log.info("📤 HTTP 200 响应已发送")

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

# ================== 资源清理 ==================
def cleanup():
    """清理所有子进程和资源"""
    global httpd, ffmpeg_proc, streamlink_proc, potplayer_proc
   
    log.info("🧹 开始清理资源...")
    shutdown_event.set()
   
    if httpd:
        try:
            httpd.shutdown()
            log.info("HTTP服务器已关闭")
        except:
            pass
   
    # 终止进程（避免僵尸进程）
    procs = [
        ('FFmpeg', ffmpeg_proc),
        ('Streamlink', streamlink_proc),
        ('PotPlayer', potplayer_proc)
    ]
   
    for name, proc in procs:
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
                log.info(f"{name} 已终止")
            except:
                try:
                    proc.kill()
                    log.info(f"{name} 已强制终止")
                except:
                    pass

# ================== 主程序 ==================
def main():
    global ffmpeg_proc, streamlink_proc, potplayer_proc

    log.info(f"🚀 程序启动 | 目标直播间: {ROOM_ID}")
    log.info(f"FFmpeg路径: {FFMPEG_PATH}")
    log.info(f"PotPlayer路径: {POTPLAYER_PATH}")
   
    # 检查依赖
    if not (os.path.isfile(FFMPEG_PATH) or FFMPEG_PATH in ['ffmpeg', 'avconv']):
        log.critical(f"❌ 找不到 ffmpeg，请确保已添加到PATH: {FFMPEG_PATH}")
        input("按回车键退出...")
        return
    if not (os.path.isfile(POTPLAYER_PATH) or 'potplayer' in POTPLAYER_PATH.lower()):
        log.critical(f"❌ 找不到 PotPlayer，请确保已添加到PATH: {POTPLAYER_PATH}")
        input("按回车键退出...")
        return

    # --- 1. 启动管道 ---
    # 【修复点1】添加 creationflags 隐藏 Streamlink 和 FFmpeg 的黑窗口
    try:
        # 配置隐藏窗口标志 (仅 Windows)
        startupinfo = None
        creationflags = 0
        if sys.platform == 'win32':
            # 方法1：使用 CREATE_NO_WINDOW (适用于没有控制台的应用)
            creationflags = 0x08000000 # subprocess.CREATE_NO_WINDOW
           
            # 方法2：使用 STARTUPINFO (兼容性更好)
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        streamlink_cmd = [
            "streamlink",
            "--stdout",
            "--loglevel", "error",
            f"https://live.bilibili.com/{ROOM_ID}",
            "best"
        ]
       
        ffmpeg_cmd = [
            FFMPEG_PATH,
            "-loglevel", "info",
            "-i", "pipe:0",
            "-vn",
            "-c:a", "aac",
            "-b:a", "128k",
            "-ar", "44100",
            "-f", "adts",
            "-"
        ]

        log.info("⚙️ 启动 Streamlink 和 FFmpeg 管道...")
       
        # 启动 Streamlink (隐藏窗口)
        streamlink_proc = subprocess.Popen(
            streamlink_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
            startupinfo=startupinfo
        )
       
        # 启动 FFmpeg (隐藏窗口)
        ffmpeg_proc = subprocess.Popen(
            ffmpeg_cmd,
            stdin=streamlink_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            creationflags=creationflags,
            startupinfo=startupinfo
        )
        streamlink_proc.stdout.close()
       
        log.info("✅ 管道启动成功")

    except Exception as e:
        log.critical(f"❌ 管道启动失败: {e}")
        input("按回车键退出...")
        return

    # --- 2. 预加载 ---
    log.info(f"⏳ 预加载 {PRELOAD_TIME} 秒音频...")
    preload_buffer = bytearray()
    start_time = time.time()

    def log_ffmpeg_stderr():
        for line in iter(ffmpeg_proc.stderr.readline, b''):
            if line:
                log.info(f"🎥 FFmpeg: {line.decode('utf-8', errors='replace').strip()}")

    # 注意：由于 stderr 现在是 PIPE，这个线程依然能读取日志，但不会显示在屏幕上
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
        # 【修复点2】添加 creationflags 隐藏 PotPlayer 的控制台（如果有的话）
        potplayer_proc = subprocess.Popen(
            [POTPLAYER_PATH, AUDIO_URL],
            creationflags=creationflags,
            startupinfo=startupinfo
        )
        log.info("▶️ PotPlayer 已启动")
    except Exception as e:
        log.critical(f"❌ 启动 PotPlayer 失败: {e}")
        cleanup()
        input("按回车键退出...")
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
        cleanup()
        log.info("👋 程序已安全退出")

if __name__ == "__main__":
    # 注册清理函数（Windows下信号处理有限）
    try:
        signal.signal(signal.SIGINT, lambda s, f: shutdown_event.set())
        signal.signal(signal.SIGTERM, lambda s, f: shutdown_event.set())
    except:
        pass
   
    main()