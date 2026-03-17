import os
import socket
import uvicorn
import threading
import webbrowser
import time

# --- Configurable Variables ---
PORT = 8420 
APP_MODULE = "glider_playground.app:app"
BROWSER_DELAY = 1.5
LOG_LEVEL = "warning"
SERVER_HOSTNAMES = ["raspberrypi", "server", "server.local"]
# ------------------------------

def open_browser(host):
    time.sleep(BROWSER_DELAY)
    url = f"http://{host}:{PORT}"
    print(f"Opening browser at {url} ...")
    webbrowser.open(url)

def main():
    # Improved detection: checks environment variable OR if hostname is in our list
    is_server_env = os.getenv("IS_SERVER") == "True"
    current_hostname = socket.gethostname().lower()
    
    is_server = is_server_env or current_hostname in SERVER_HOSTNAMES
    
    if is_server:
        host = "0.0.0.0"
        print(f"Running in Server Mode (0.0.0.0) on {current_hostname}")
    else:
        host = "127.0.0.1"
        print("Running in Local Mode (127.0.0.1)")

    print("Starting Glider Playground...")
    
    if not is_server:
        threading.Thread(target=open_browser, args=(host,), daemon=True).start()
    
    # reload=False is safer for a background service
    uvicorn.run(APP_MODULE, host=host, port=PORT, log_level=LOG_LEVEL, reload=False)

if __name__ == "__main__":
    main()