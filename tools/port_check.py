"""启动前探端口：判断是「我们的服务已经在跑」还是「端口被别的程序占了」。

退出码：0 = 本程序已在运行（直接开浏览器）；1 = 端口空闲；2 = 被其他程序占用
"""
import json
import socket
import sys
import urllib.request

port = int(sys.argv[1]) if len(sys.argv) > 1 else 8799

s = socket.socket()
s.settimeout(0.5)
busy = s.connect_ex(("127.0.0.1", port)) == 0
s.close()
if not busy:
    sys.exit(1)

try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
        data = json.loads(r.read().decode("utf-8", "ignore"))
    sys.exit(0 if data.get("version") == "1.0.0" and "yt_dlp" in data else 2)
except Exception:
    sys.exit(2)
