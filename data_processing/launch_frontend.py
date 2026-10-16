"""
70城房价信息平台 - 一键启动器
使用已发布数据启动 HTTP 服务；后台按期隔离更新与校验，成功后发布。
"""

import subprocess
import sys
import os
import webbrowser
import threading
import io
import json
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

# ─── 配置 ───
PORT = 8765
URL = f"http://127.0.0.1:{PORT}"

# 项目根目录 (exe所在目录 或 脚本所在目录)
if getattr(sys, 'frozen', False):
    PROJECT_ROOT = Path(sys.executable).parent
else:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent

FRONTEND_DIR = PROJECT_ROOT / "data_processing" / "frontend"

UPDATE_SCRIPT = PROJECT_ROOT / "data_processing" / "update_data.py"
POLICY_PATH = PROJECT_ROOT / "data_processing" / "update_policy.json"
STARTUP_SOURCES = ('cih',)   # 中指界面为日度口径：每次打开项目强制爬取一次
startup_done = threading.Event()


def setup_encoding():
    """设置控制台编码为UTF-8"""
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
        os.system('chcp 65001 >nul 2>&1')


def open_browser():
    """延迟打开浏览器"""
    import time
    time.sleep(1.5)  # 等待HTTP服务器启动
    webbrowser.open(URL)


class QuietHandler(SimpleHTTPRequestHandler):
    """静默的HTTP请求处理器"""
    def log_message(self, format, *args):
        # 只在第一次请求时打印
        if not hasattr(self, '_logged'):
            self._logged = True
            print(f"[提示] 页面已加载: {URL}")

    def end_headers(self):
        # 添加 CORS 头
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-cache')
        SimpleHTTPRequestHandler.end_headers(self)


def start_server():
    """启动HTTP服务器。

    端口策略: 首选 8765; 若被占用(WinError 10048)或被系统保留
    (WinError 10013, 常见于 Hyper-V/WSL 保留端口段, 多项目同开时易触发),
    自动顺延到 8766~8775, 最后交给系统随机分配(端口 0)。
    前端全部使用相对路径, 任意端口均可正常工作。
    """
    global URL

    candidates = [PORT] + [PORT + i for i in range(1, 11)] + [0]
    server = None
    port = None
    last_err = None
    for cand in candidates:
        try:
            server = HTTPServer(('127.0.0.1', cand), partial(QuietHandler,directory=str(FRONTEND_DIR)))
            port = server.server_address[1]  # cand 为 0 时由系统分配实际端口
            break
        except OSError as e:
            last_err = e
            server = None
            continue
    if server is None:
        print("[错误] 无法绑定任何端口(8765~8775 及系统分配均失败)")
        print(f"       最后错误: {last_err}")
        print("       WinError 10013 常见于端口被 Hyper-V/WSL 保留或被防火墙拦截,")
        print("       可尝试: 重启电脑, 或以管理员执行  net stop winnat && net start winnat")
        input("按回车键退出...")
        return
    if port != PORT:
        print(f"[提示] 端口 {PORT} 不可用(被占用或被系统保留)，自动改用 {port}")
    URL = f"http://127.0.0.1:{port}"

    # 在另一个线程中打开浏览器
    browser_thread = threading.Thread(target=open_browser, daemon=True)
    browser_thread.start()
    updater_stop=threading.Event()
    startup_thread=threading.Thread(target=startup_refresh,args=(updater_stop,),daemon=True)
    startup_thread.start()
    updater_thread=threading.Thread(target=refresh_loop,args=(updater_stop,),daemon=True)
    updater_thread.start()

    try:
        print(f"[2/3] HTTP服务已启动")
        print(f"[3/3] 正在打开浏览器...")
        print()
        print("-" * 46)
        print(f"  访问地址: {URL}")
        print(f"  按 Ctrl+C 停止服务")
        print("-" * 46)
        print()
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[提示] 正在关闭服务...")
        updater_stop.set()
        server.server_close()
        print("       服务已关闭，再见！")
    except Exception as e:
        print(f"[错误] 服务异常中断: {e}")
        input("按回车键退出...")


def policy_enabled():
    try:
        return json.loads(POLICY_PATH.read_text(encoding='utf-8')).get('enabled', True)
    except Exception:
        return True


def startup_command():
    cmd = [sys.executable, str(UPDATE_SCRIPT), '--dashboard-only']
    for source in STARTUP_SOURCES:
        cmd += ['--sources', source]
    cmd.append('--force')
    return cmd


def startup_refresh(stop):
    """打开项目即爬取中指官网：中指界面为日度口径，启动时强制刷新一次。

    与周期更新相同的隔离构建模式：失败时保留已发布旧数据；子进程化保证
    绝不阻塞界面打开；finally 置位 startup_done，让周期更新循环随后接手。
    """
    logdir = PROJECT_ROOT / 'output' / 'update_logs'
    logdir.mkdir(parents=True, exist_ok=True)
    try:
        if not policy_enabled():
            print('[启动更新] 数据更新已被策略禁用，跳过中指启动爬取')
            return
        print('[启动更新] 中指界面为日度数据：正在后台爬取中指官网（当前页面先用已发布数据，成功后自动发布刷新）')
        with (logdir / 'launcher_startup.log').open('a', encoding='utf-8') as log:
            process = subprocess.Popen(startup_command(), cwd=PROJECT_ROOT, stdout=log,
                                       stderr=subprocess.STDOUT,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            while process.poll() is None:
                if stop.wait(1):
                    return
        print('[启动更新] 中指启动爬取结束（详见 output/update_logs/launcher_startup.log）')
    except Exception as exc:
        print('[启动更新] 暂未执行：' + str(exc))
    finally:
        startup_done.set()


def main():
    setup_encoding()
    # Serve the last validated dataset immediately. All refreshing happens in an isolated build.
    if not (FRONTEND_DIR/'intersection_housing_data.json').exists():
        print('[错误] 缺少初始数据，请先运行 data_processing/update_data.py --offline')
        return
    start_server()


def refresh_loop(stop):
    script=PROJECT_ROOT/'data_processing/update_data.py'
    policy_path=PROJECT_ROOT/'data_processing/update_policy.json'
    # 启动爬取（中指日度强制刷新）先行完成或超时，避免与首次到期检查争写锁。
    startup_done.wait(2400)
    while not stop.is_set():
        try:
            policy=json.loads(policy_path.read_text(encoding='utf-8'))
            if policy.get('enabled',True):
                print('[更新] 正在后台检查数据源；当前页面继续使用已发布数据')
                # Popen lets server shutdown stop the next due check without blocking the HTTP UI.
                logdir=PROJECT_ROOT/'output/update_logs';logdir.mkdir(parents=True,exist_ok=True)
                with (logdir/'launcher.log').open('a',encoding='utf-8') as log:
                    process=subprocess.Popen([sys.executable,str(script),'--dashboard-only'],cwd=PROJECT_ROOT,stdout=log,stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                    # Allow an in-flight isolated update to finish after closing the server.
                    while process.poll() is None:
                        if stop.wait(1):return
            interval=max(60,int(policy.get('check_interval_seconds',3600)))
        except Exception as exc:
            print('[更新] 暂未执行：'+str(exc));interval=3600
        stop.wait(interval)


if __name__ == '__main__':
    main()
