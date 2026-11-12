# تشغيل خدمات الديمو بأمر واحد دون حذف بيانات العيادة.
import argparse
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import httpx
from dotenv import load_dotenv


# نفحص المنافذ أولًا كي لا نقتل أو نستبدل عملية يملكها المستخدم.
def free_port(port):
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            raise RuntimeError(f"Port {port} is already in use. Stop your old demo yourself, or use its services.") from None


# ينتظر صحة المساعد والعيادة؛ لا يحجز ولا يستدعي endpoints إدارية.
def wait_ready(port, process):
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError(f"Service on {port} exited. Read its error above.")
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).is_success:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    raise RuntimeError(f"Service on {port} did not become ready.")


# Ctrl+C يوقف أبناء هذا المشغل فقط، ولا يلمس خوادم قديمة أو ملفات المستخدم.
def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Validate imports and print commands without starting services")
    parser.add_argument("--demo-date", help="Optional clinic calendar date, e.g. 2026-11-02")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    load_dotenv(root / ".env")
    commands = [[sys.executable, "-B", "-m", "uvicorn", "services.clinic_api:app", "--host", "127.0.0.1", "--port", "8101"],
                [sys.executable, "-B", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"]]
    if args.check:
        import fastapi, uvicorn, mcp, openai, langgraph
        for command in commands:
            print(subprocess.list2cmdline(command))
        print("Imports ready. No service started; no clinic data changed.")
        return
    if not os.getenv("OPENROUTER_API_KEY"):
        raise RuntimeError("Set OPENROUTER_API_KEY in kit/.env first. Never commit .env.")
    for port in (8101, 8000):
        free_port(port)
    env = {**os.environ, "APP_URL": "http://127.0.0.1:8000",
           "CLINIC_API_URL": "http://127.0.0.1:8101", "PYTHONUTF8": "1"}
    if args.demo_date:
        from datetime import date
        date.fromisoformat(args.demo_date)
        env["CLINIC_TODAY"] = args.demo_date
    children = []
    try:
        for index, command in enumerate(commands):
            child = subprocess.Popen(command, cwd=root, env=env)
            children.append(child)
            wait_ready((8101, 8000)[index], child)
        print("Open http://127.0.0.1:8000 — Ctrl+C stops only this demo's child processes.", flush=True)
        while all(child.poll() is None for child in children):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()


if __name__ == "__main__":
    main()
