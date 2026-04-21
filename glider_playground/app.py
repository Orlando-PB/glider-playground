import uvicorn
import platform
import subprocess
import sys
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import os
from pathlib import Path
import time
import logging
import threading
import httpx

from . import plot_logic
from . import spatial_logic
from . import jelly_logic

app = FastAPI()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

@app.middleware("http")
async def log_request_timing(request, call_next):
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    
    response.headers["X-Process-Time"] = str(process_time)
    return response


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

state = {
    "DATA_DIR": Path.cwd() / "data"
}

data_lock = threading.Lock()

STATIC_DIR.mkdir(exist_ok=True)
state["DATA_DIR"].mkdir(exist_ok=True)
    
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/")
def read_root(): 
    return FileResponse(str(STATIC_DIR / "index.html"))

@app.post("/api/open_folder")
def open_data_folder():
    system = platform.system()
    folder_path = ""
    
    try:
        if system == "Darwin":  
            cmd = [
                "osascript", "-e", 
                "tell application (path to frontmost application as text) to return POSIX path of (choose folder)"
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            folder_path = result.stdout.strip()
            
        elif system == "Windows":  
            cmd = [
                sys.executable, "-c",
                "import tkinter as tk; from tkinter import filedialog; "
                "root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True); "
                "print(filedialog.askdirectory())"
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            folder_path = result.stdout.strip()
            
        else:  
            try:
                result = subprocess.run(["zenity", "--file-selection", "--directory"], capture_output=True, text=True)
                folder_path = result.stdout.strip()
            except FileNotFoundError:
                cmd = [
                    sys.executable, "-c",
                    "import tkinter as tk; from tkinter import filedialog; "
                    "root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True); "
                    "print(filedialog.askdirectory())"
                ]
                result = subprocess.run(cmd, capture_output=True, text=True)
                folder_path = result.stdout.strip()

        if folder_path and os.path.isdir(folder_path):
            state["DATA_DIR"] = Path(folder_path)
            return {"status": "success", "path": str(state["DATA_DIR"])}
        else:
            return {"status": "cancelled"}

    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/set_folder")
def set_folder(path: str):
    if not path or not os.path.isdir(path):
        return {"status": "error", "message": "Folder not found"}
    state["DATA_DIR"] = Path(path)
    return {"status": "success", "path": str(state["DATA_DIR"])}

@app.get("/api/map")
def get_map(filename: str):
    with data_lock:
        return spatial_logic.generate_map_image(str(state["DATA_DIR"] / filename))

@app.get("/api/3d_data")
def get_3d_data(filename: str):
    with data_lock:
        return spatial_logic.generate_3d_data(str(state["DATA_DIR"] / filename))

@app.get("/api/location")
def get_location(filename: str):
    with data_lock:
        return spatial_logic.get_location_summary(str(state["DATA_DIR"] / filename))

@app.get("/api/files")
def get_files():
    data_dir = state["DATA_DIR"]
    if not data_dir.exists():
        return {"files": []}
        
    return {"files": [f.relative_to(data_dir).as_posix() for f in data_dir.rglob('*.nc')]}

@app.get("/api/variables")
def get_variables(filename: str):
    with data_lock:
        return {"variables": plot_logic.get_variables(str(state["DATA_DIR"] / filename))}

@app.get("/api/dataset_info")
def get_dataset_info(filename: str):
    with data_lock:
        return plot_logic.get_dataset_info(str(state["DATA_DIR"] / filename))

@app.get("/api/profiles")
def get_profiles(filename: str):
    with data_lock:
        return plot_logic.get_profiles(str(state["DATA_DIR"] / filename))
    
@app.get("/api/config")
def get_config():
    return {"is_server": os.getenv("IS_SERVER") == "True"}

@app.get("/api/plot_data")
def get_plot_data(
    filename: str, x_var: str, y_var: str, c_var: str = "",
    apply_qc: bool = False, qc_flags: str = "1,2,5,8", highlight_qc: bool = False, filter_time: bool = True,
    profile_num: float = None
):
    with data_lock:
        return plot_logic.get_plot_data_json(
            str(state["DATA_DIR"] / filename), x_var, y_var, c_var,
            apply_qc=apply_qc, qc_flags=qc_flags, highlight_qc=highlight_qc, filter_time=filter_time,
            profile_num=profile_num
        )

@app.get("/api/plot_data_bounds")
def get_plot_data_bounds(
    filename: str, x_var: str, y_var: str, c_var: str = "",
    apply_qc: bool = False, qc_flags: str = "1,2,5,8", highlight_qc: bool = False, filter_time: bool = True,
    x_min: float = None, x_max: float = None, y_min: float = None, y_max: float = None,
    profile_num: float = None
):
    with data_lock:
        return plot_logic.get_plot_data_bounds(
            str(state["DATA_DIR"] / filename), x_var, y_var, c_var,
            apply_qc=apply_qc, qc_flags=qc_flags, highlight_qc=highlight_qc, filter_time=filter_time,
            x_min=x_min, x_max=x_max, y_min=y_min, y_max=y_max,
            profile_num=profile_num
        )
    
@app.post("/api/download_demo")
async def download_demo_files():
    demo_files = [
        "https://linkedsystems.uk/erddap/files/Public_OG1_Data_001_Recovery/Nelson_20240528/Nelson_646.nc",
        "https://linkedsystems.uk/erddap/files/Public_Glider_Data_0711/Nelson_20240528/Nelson_646_R.nc"
    ]
    
    data_dir = state["DATA_DIR"]
    data_dir.mkdir(exist_ok=True)
    
    try:
        async with httpx.AsyncClient() as client:
            for url in demo_files:
                filename = url.split("/")[-1]
                target_path = data_dir / filename
                
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    with open(target_path, "wb") as f:
                        async for chunk in response.aiter_bytes():
                            f.write(chunk)
                            
        return {"status": "success"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/jelly/key_status")
def jelly_key_status():
    return {"has_key": jelly_logic.has_api_key()}

@app.post("/api/jelly/set_key")
async def jelly_set_key(request: Request):
    body = await request.json()
    key = (body.get("key") or "").strip()
    if not key or len(key) < 10:
        return {"status": "error", "message": "Key looks too short."}
    jelly_logic.set_api_key(key)
    return {"status": "success"}

@app.post("/api/jelly/delete_key")
def jelly_delete_key():
    jelly_logic.delete_api_key()
    return {"status": "success"}

@app.post("/api/jelly/chat")
async def jelly_chat(request: Request):
    body = await request.json()
    return await jelly_logic.chat(body)

if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8420, reload=True, access_log=False)