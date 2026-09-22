import webview
import threading
import time
from work.ticket_server import Handler, ThreadingHTTPServer

def start_server():
    # 启动你的本地 HTTP 服务
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    server.serve_forever()

if __name__ == '__main__':
    # 1. 在后台线程启动 12306 接口服务
    t = threading.Thread(target=start_server, daemon=True)
    t.start()
    
    # 2. 等待服务启动 (稍微等一秒)
    time.sleep(1)
    
    # 3. 创建并启动桌面窗口，直接加载本地服务地址
    webview.create_window('12306 抢票与路线助手', 'http://127.0.0.1:8765/', width=1280, height=800)
    webview.start()