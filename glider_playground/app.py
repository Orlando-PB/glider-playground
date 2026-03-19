import uvicorn
import platform
import subprocess
import sys
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import os
from pathlib import Path

from . import plot_logic
from . import map_logic

app = FastAPI()

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

# Use a dictionary to store the current directory state dynamically
state = {
    "DATA_DIR": Path.cwd() / "data"
}

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
        if system == "Darwin":  # macOS Native Dialog
            # This AppleScript forces the native Mac folder picker to appear in front of the browser
            cmd = [
                "osascript", "-e", 
                "tell application (path to frontmost application as text) to return POSIX path of (choose folder)"
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            folder_path = result.stdout.strip()
            
        elif system == "Windows":  # Windows Tkinter Dialog
            cmd = [
                sys.executable, "-c",
                "import tkinter as tk; from tkinter import filedialog; "
                "root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True); "
                "print(filedialog.askdirectory())"
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            folder_path = result.stdout.strip()
            
        else:  # Linux (Try Zenity, fallback to Tkinter)
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

@app.get("/api/map")
def get_map(filename: str):
    return map_logic.generate_map_image(str(state["DATA_DIR"] / filename))

@app.get("/api/files")
def get_files():
    data_dir = state["DATA_DIR"]
    if not data_dir.exists():
        return {"files": []}
    return {"files": [f for f in os.listdir(data_dir) if f.endswith('.nc')]}

@app.get("/api/variables")
def get_variables(filename: str):
    return {"variables": plot_logic.get_variables(str(state["DATA_DIR"] / filename))}

@app.get("/api/dataset_info")
def get_dataset_info(filename: str):
    return plot_logic.get_dataset_info(str(state["DATA_DIR"] / filename))

@app.get("/api/sparkline")
def get_sparkline(filename: str, x_var: str, y_var: str):
    return plot_logic.generate_sparkline(str(state["DATA_DIR"] / filename), x_var, y_var)
    
@app.get("/api/config")
def get_config():
    return {"is_server": os.getenv("IS_SERVER") == "True"}

@app.get("/api/hover")
def get_hover(
    filename: str, x_var: str, y_var: str, x_val: float, y_val: float, 
    c_var: str = "", is_x_dt: str = 'false', x_min: float = 0.0, x_max: float = 0.0, 
    y_min: float = 0.0, y_max: float = 0.0
):
    is_dt = is_x_dt.lower() == 'true'
    return plot_logic.get_nearest_point(str(state["DATA_DIR"] / filename), x_var, y_var, c_var, x_val, y_val, is_dt, x_min, x_max, y_min, y_max)

@app.get("/api/plot")
def get_plot(
    filename: str, x_var: str, y_var: str, c_var: str = "", cmap: str = "viridis", 
    plot_delta: bool = False, delta_axis: str = "x", invert_y: bool = False, 
    trim_start: str = None, trim_end: str = None, 
    y_trim_min: str = None, y_trim_max: str = None,
    c_trim_min: str = None, c_trim_max: str = None,
    apply_qc: bool = False, qc_flags: str = "1,2,5,8",
    plot_all: bool = False, filter_time: bool = True
):
    return plot_logic.generate_plot(
        str(state["DATA_DIR"] / filename), x_var, y_var, c_var, cmap=cmap, 
        plot_delta=plot_delta, delta_axis=delta_axis, invert_y=invert_y, 
        trim_start=trim_start, trim_end=trim_end,
        y_trim_min=y_trim_min, y_trim_max=y_trim_max,
        c_trim_min=c_trim_min, c_trim_max=c_trim_max,
        apply_qc=apply_qc, qc_flags=qc_flags, plot_all=plot_all, filter_time=filter_time
    )

import httpx # You may need to: pip install httpx

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
if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8420, reload=True)