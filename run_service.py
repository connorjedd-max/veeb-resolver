"""Supervise both processes so a failed PO-token provider cannot look healthy."""
import os
import signal
import subprocess
import sys
import time
import urllib.request


def run():
    children = []
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        provider = subprocess.Popen(['node', '/opt/bgutil/server/build/main.js', '--host', '127.0.0.1', '--port', '4416'])
        children.append(provider)
        for _ in range(100):
            if stopping or provider.poll() is not None:
                raise RuntimeError('PO-token provider stopped before readiness')
            try:
                with urllib.request.urlopen('http://127.0.0.1:4416/ping', timeout=.4) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(.2)
        else:
            raise RuntimeError('PO-token provider did not become ready')
        if os.environ.get('VEEB_ROLE', 'resolver') == 'agent':
            command = [sys.executable, '/app/source_agent.py', *sys.argv[1:]]
        else:
            command = [sys.executable, '-m', 'uvicorn', 'veeb_resolver:app', '--host', '0.0.0.0',
                       '--port', os.environ.get('PORT', '10000'), '--workers', '1', '--timeout-graceful-shutdown', '10']
        service = subprocess.Popen(command)
        children.append(service)
        while not stopping:
            if service.poll() is not None:
                return service.returncode
            if provider.poll() is not None:
                raise RuntimeError('PO-token provider exited; restarting the service is required')
            time.sleep(.2)
        return 0
    finally:
        for process in reversed(children):
            if process.poll() is None:
                process.terminate()
        for process in reversed(children):
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == '__main__':
    sys.exit(run())
