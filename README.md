# Glider Playground

A fast, web-based NetCDF explorer for viewing and validating glider data. This tool provides a highly responsive UI to explore oceanographic .nc files without running computationally heavy processing steps on the fly. It was built by me as a simple tool to learn about glider data at NOC (National Ocenography Center).

## Prerequisites

It is highly recommended to run this project inside a Python virtual environment to keep its dependencies isolated from the rest of your system.

1. Open your terminal and navigate to the project folder.
2. Create a virtual environment named .venv:
   ```python -m venv .venv```    
3. Activate the virtual environment:
   ```source .venv/bin/activate ```   
(Note: You will need to run the activation command any time you open a new terminal window to work on this project).

## Installation

Ensure you have Python 3.9+ installed and your virtual environment is active. Navigate to this directory in your terminal and run:

```pip install -e . ```

The -e flag installs the package in "editable" mode. This means any changes you make to the Python code will be immediately recognised without needing to reinstall the package.

## How to Run

Once installed, you can start the application from anywhere in your terminal (as long as your virtual environment is active) by simply typing:

```glider-playground``` 

This will load up the local server and automatically open the application in your default web browser. To stop the server, return to the terminal and press Ctrl+C.

## How to use

.nc data files must be placed inside a data folder. There is a button to open this folder in the browser. You can then see the variables, and create X,Y plots, and also colour it by another variable. If the data has _QC flags, it you can choose which points to plot. the default is anything with QC 5 or less. You can zoom into a plot by dragging a square on it. There is a reset button at the bottom

## Uninstalling

If you ever wish to remove the application and its terminal command from your environment, simply run:

```pip uninstall glider-playground``` 