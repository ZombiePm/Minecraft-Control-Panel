import os
import subprocess
import threading
import time
import logging
from pathlib import Path
import psutil

from flask import Flask, render_template, request, redirect, session, jsonify, send_file
from flask_socketio import SocketIO, emit, join_room

from auth import login_required

# ------------------------ CONFIG ------------------------ #

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = 'a56810c5eb9469da4771732cc9fac8a305112286738b40caa07759e1463d6563'

MC_DIR = '/media/s/minecraft/'
MC_JAR = 'spigot-1.20.1.jar'
LOG_FILE = '/media/s/minecraft/logs/latest.log'
PID_FILE = os.path.join(MC_DIR, 'mc.pid')
HOST = '0.0.0.0'
PORT = 8090
JAVA_XMX = '2G'
JAVA_XMS = '1G'
JAVA_BIN = '/usr/bin/java'
ADMIN_PASSWORD = 'AwK1MvcjEZy35x6xxRzvUw=='
MC_USER = 'mcuser'

socketio = SocketIO(app, cors_allowed_origins="*", async_mode='eventlet')

# ------------------------ GLOBALS ------------------------ #

PROCESS = None
OUTPUT_THREAD = None
STOP_READING = threading.Event()

# ------------------------ HELPERS ------------------------ #

def is_running():
    global PROCESS
    return PROCESS is not None and PROCESS.poll() is None

def kill_mcuser_processes(user=MC_USER):
    """Force kill all processes belonging to a given user."""
    try:
        for proc in psutil.process_iter(['pid', 'username', 'name']):
            if proc.info['username'] == user:
                try:
                    proc.kill()
                    logger.warning(f"Killed {proc.info['name']} (PID {proc.info['pid']})")
                except Exception as e:
                    logger.error(f"Failed to kill {proc.info['name']} (PID {proc.info['pid']}): {e}")
        logger.info(f"All processes for user {user} have been terminated")
    except Exception as e:
        logger.error(f"Error killing processes for user {user}: {e}")

def force_kill(process):
    """Force kill server process, all children, remove PID file"""
    try:
        STOP_READING.set()
        if OUTPUT_THREAD:
            OUTPUT_THREAD.join(timeout=2)
        parent = psutil.Process(process.pid)
        for child in parent.children(recursive=True):
            try:
                child.kill()
            except Exception as e:
                logger.error(f"Failed to kill child {child.pid}: {e}")
        try:
            parent.kill()
            logger.warning(f"Process {process.pid} force killed")
        except Exception as e:
            logger.error(f"Failed to kill main process {process.pid}: {e}")

        # Убиваем все процессы mcuser
        kill_mcuser_processes(MC_USER)

        # Удаляем PID файл
        if os.path.exists(PID_FILE):
            try:
                os.remove(PID_FILE)
                logger.info(f"Removed PID file {PID_FILE}")
            except Exception as e:
                logger.error(f"Failed to remove PID file: {e}")

    except Exception as e:
        logger.error(f"Error force killing server: {e}")
    finally:
        global PROCESS
        PROCESS = None
        STOP_READING.clear()

def get_server_stats():
    stats = {
        "running": is_running(),
        "pid": None,
        "cpu_percent": None,
        "memory_mb": None,
        "timestamp": time.time()
    }
    if is_running():
        try:
            p = psutil.Process(PROCESS.pid)
            stats["pid"] = PROCESS.pid
            stats["cpu_percent"] = p.cpu_percent()
            stats["memory_mb"] = round(p.memory_info().rss / 1024 / 1024, 2)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return stats

def tail_log(file_path, lines=20):
    try:
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            return ''.join(f.readlines()[-lines:])
    except Exception as e:
        return f"Error reading log: {str(e)}"

def read_server_output():
    global PROCESS
    while is_running() and not STOP_READING.is_set():
        if PROCESS.stdout:
            line = PROCESS.stdout.readline()
            if line:
                try:
                    with open(LOG_FILE, 'a', encoding='utf-8') as f:
                        f.write(line)
                except Exception:
                    pass
                socketio.emit('mc_output', {'line': line.rstrip()}, namespace='/console')
        else:
            time.sleep(0.1)

# ------------------------ ROUTES ------------------------ #

@app.route('/')
@login_required
def index():
    return render_template('index.html', running=is_running())

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        password = request.form['password']
        if password == ADMIN_PASSWORD:
            session['logged'] = True
            return redirect('/')
        else:
            return render_template('login.html', error=True)
    return render_template('login.html', error=False)

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')

@app.route('/start', methods=['POST'])
@login_required
def start():
    global PROCESS, OUTPUT_THREAD
    if not is_running():
        try:
            os.makedirs(MC_DIR, exist_ok=True)
            cmd = [JAVA_BIN, f"-Xmx{JAVA_XMX}", f"-Xms{JAVA_XMS}", "-jar", MC_JAR, "nogui"]
            PROCESS = subprocess.Popen(
                cmd,
                cwd=MC_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.PIPE,
                text=True,
                bufsize=1,
                universal_newlines=True
            )
            # Сохраняем PID в файл
            with open(PID_FILE, 'w') as f:
                f.write(str(PROCESS.pid))

            STOP_READING.clear()
            OUTPUT_THREAD = threading.Thread(target=read_server_output, daemon=True)
            OUTPUT_THREAD.start()
            logger.info(f"Minecraft server started with PID {PROCESS.pid}")
        except Exception as e:
            logger.error(f"Failed to start server: {e}")
    return redirect('/')

@app.route('/stop', methods=['POST'])
@login_required
def stop():
    global PROCESS
    if is_running():
        force_kill(PROCESS)
    else:
        kill_mcuser_processes(MC_USER)
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
    return redirect('/')

@app.route('/restart', methods=['POST'])
@login_required
def restart():
    global PROCESS
    if is_running():
        try:
            PROCESS.stdin.write("stop\n")
            PROCESS.stdin.flush()
            PROCESS.wait(timeout=5)
        except (subprocess.TimeoutExpired, BrokenPipeError):
            force_kill(PROCESS)
        finally:
            PROCESS = None
            time.sleep(1)
    kill_mcuser_processes(MC_USER)
    return redirect('/')

@app.route('/api/status')
@login_required
def api_status():
    return jsonify(get_server_stats())

@app.route('/download/log')
@login_required
def download_log():
    try:
        return send_file(LOG_FILE, as_attachment=True, download_name="mc.log")
    except Exception as e:
        return str(e), 404

# ------------------------ SOCKETIO ------------------------ #

@socketio.on('connect', namespace='/console')
def handle_connect():
    logger.info(f"Client connected: {request.sid}")

@socketio.on('join', namespace='/console')
def handle_join(data):
    join_room(request.sid)
    log_content = tail_log(LOG_FILE, 50)
    for line in log_content.split('\n'):
        if line.strip():
            emit('mc_output', {'line': line})

@socketio.on('mc_input', namespace='/console')
def handle_mc_input(data):
    global PROCESS
    if is_running() and 'cmd' in data:
        try:
            cmd = data['cmd'].strip()
            PROCESS.stdin.write(cmd + '\n')
            PROCESS.stdin.flush()
            logger.info(f"Command sent: {cmd}")
        except Exception as e:
            logger.error(f"Error sending command: {e}")

# ------------------------ MAIN ------------------------ #

if __name__ == '__main__':
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    Path(LOG_FILE).touch(exist_ok=True)
    socketio.run(app, host=HOST, port=PORT, debug=True)
