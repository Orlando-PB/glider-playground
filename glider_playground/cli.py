import uvicorn
import webbrowser
import threading
import time

# --- Configurable Variables ---
HOST = "127.0.0.1"
PORT = 8420  # Changed to avoid conflicts with other apps
APP_MODULE = "glider_playground.app:app"
BROWSER_DELAY = 1.5
LOG_LEVEL = "warning"
# ------------------------------

def open_browser():
    time.sleep(BROWSER_DELAY)
    url = f"http://{HOST}:{PORT}"
    print(f"Opening browser at {url} ...")
    webbrowser.open(url)

def main():
    print("Starting Glider Playground...")
    print("Press Ctrl+C in this terminal to safely exit.")
    
    threading.Thread(target=open_browser, daemon=True).start()
    uvicorn.run(APP_MODULE, host=HOST, port=PORT, log_level=LOG_LEVEL, reload=True)

if __name__ == "__main__":
    main()