# Glider Playground

A fast, web-based NetCDF explorer for viewing and validating glider data. This tool provides a highly responsive UI to explore oceanographic `.nc` files without running computationally heavy processing steps on the fly. It was built as a simple tool to learn about glider data at NOC (National Oceanography Centre).

![Glider Playground Home View](glider_playground/static/home_view.png)

## Prerequisites

It is highly recommended to run this project inside a Python virtual environment to keep its dependencies isolated from the rest of your system.

1. Open your terminal and navigate to the project folder.
2. Create a virtual environment named `.venv`:
   ```bash
   python -m venv .venv
   ```    
3. Activate the virtual environment:
   ```bash
   source .venv/bin/activate
   ```   
*(Note: You will need to run the activation command any time you open a new terminal window to work on this project).*

## Installation

Ensure you have Python 3.9+ installed and your virtual environment is active. Navigate to this directory in your terminal and run:

```bash
pip install -e .
```

The `-e` flag installs the package in "editable" mode. This means any changes you make to the Python code will be immediately recognised without needing to reinstall the package.

## How to Run

Once installed, you can start the application from anywhere in your terminal (as long as your virtual environment is active) by simply typing:

```bash
glider-playground
``` 

This will load up the local server and automatically open the application in your default web browser. To stop the server, return to the terminal and press `Ctrl+C`.

## How to Use

### Loading Data
`.nc` data files must be placed inside a local `data` folder. You can click the folder icon in the top navigation bar to easily open this directory on your machine. Once files are added, select your target dataset from the dropdown.

### Views & Plotting
* **Basic Plotting:** Select your X and Y variables. You can optionally select a third variable to map to the **Colour** and choose a specific colourmap. 
* **Presets:** Use the **View** dropdown to select some built-in oceanographic presets (e.g., Thermal Structure, Salinity Profile). You can also configure your own preferred axes and colourmaps, name them, and click **Save** to create Custom Views for future use.

### Quality Control (QC) Filtering

If your dataset contains Argo standard `_QC` variables, the tool will automatically clean the plot. 
* You can control exactly which data points are rendered by providing a comma-separated list of flags (the default is `1,2,5,8` to include good, probably good, and interpolated data). This is applied to all axis.

### Interaction & Analysis
* **Zoom:** Click and drag a box directly on the plot to zoom in. You can also use the interactive range sliders on the X and Y axes to precisely trim the data limits. Click the **Reset Lims** button at the bottom to return to the full overview.
* **Z Zoom:** Drag the slider on the right to adjust the colour range to focus on.
* **Data Inspector:** Click a point to trigger the Inspector. This will reveal its exact values in the sude bar, and show its location on the mini-map.
* **Axis Controls:** Use the **Invert** checkbox to flip the Y-axis (useful for depth or pressure). Should automatically turn on in most cases. The **Delta** checkbox can be used plot the difference between variables (need same units)

### Exporting
* **Plot All:** By default, the tool limits rendering to 259,about 250,000 points to maintain a fast, responsive UI. Tick the **Plot All** checkbox to bypass this limit and render every single data point for a high-resolution view.
* **Download:** Click the **Download** button in the top right to save the plot as a PNG file.

## Uninstalling

If you ever wish to remove the application and its terminal command from your environment, simply run:

```bash
pip uninstall glider-playground
``` 
