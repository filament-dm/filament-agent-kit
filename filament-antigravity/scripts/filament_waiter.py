#!/usr/bin/env python3
"""Static zero-token Filament wake waiter for Google Antigravity."""
import json
import os
import sys
import time
import urllib.error
import urllib.request

WAKE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "filament_wake.json")

def main():
    with open(WAKE_FILE, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    url = cfg["wake_url"]
    token = cfg["wake_token"]
    headers = {
        "Authorization": f"Wake {token}",
        "User-Agent": "Antigravity-Filament-Waiter/1.0",
    }
    d = 1
    ok = False
    n = 0
    while True:
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=75) as resp:
                code = resp.getcode()
                body = resp.read().decode("utf-8", errors="replace").strip()
                if code == 200:
                    data = json.loads(body)
                    if data.get("pending", 0) == 0:
                        d = 1
                        ok = True
                        n = 0
                        continue
                    if data.get("pending", 0) > 0:
                        print(f"WAKE {body} 200", flush=True)
                        sys.exit(0)
                ok = True
                n = 0
                time.sleep(d)
                if d < 60:
                    d *= 2
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace").strip()
            if e.code == 401:
                print("WAKE_URL_EXPIRED: call get_wake_url and start the new waiter", flush=True)
                sys.exit(2)
            if e.code == 410:
                print(f"WAKE_REFUSED: {err_body} 410", flush=True)
                sys.exit(3)
            if e.code == 403:
                print(f"WAKE_FORBIDDEN: {err_body} 403", flush=True)
                sys.exit(4)
            ok = True
            n = 0
            time.sleep(d)
            if d < 60:
                d *= 2
        except Exception:
            n += 1
            if not ok and n >= 3:
                print("WAKE_UNREACHABLE: this shell cannot reach the wake URL; wait with poll_work instead", flush=True)
                sys.exit(5)
            time.sleep(d)
            if d < 60:
                d *= 2

if __name__ == "__main__":
    main()
