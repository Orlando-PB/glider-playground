# Glider Playground

A fast, web-based NetCDF explorer for viewing and validating glider data. This tool provides a highly responsive UI to explore oceanographic .nc files without running computationally heavy processing steps on the fly.

## Prerequisites

It is highly recommended to run this project inside a Python virtual environment to keep its dependencies isolated from the rest of your system.

1. Open your terminal and navigate to the project folder.
2. Create a virtual environment named .venv:
   ```bash    python3 -m venv .venv```    
3. Activate the virtual environment:
   ```bash    source .venv/bin/activate ```   
(Note: You will need to run the activation command any time you open a new terminal window to work on this project).

## Installation

Ensure you have Python 3.9+ installed and your virtual environment is active. Navigate to this directory in your terminal and run:

```bash pip install -e . ```

The -e flag installs the package in "editable" mode. This means any changes you make to the Python code will be immediately recognised without needing to reinstall the package.

## How to Run

Once installed, you can start the application from anywhere in your terminal (as long as your virtual environment is active) by simply typing:

```bash glider-playground``` 

This will spin up the local server and automatically open the application in your default web browser. To stop the server, return to the terminal and press Ctrl+C.

## How it Works (Under the Hood)

When you run the installation command, Python's package manager reads the pyproject.toml file. It spots the [project.scripts] section and automatically generates a terminal command called glider-playground.

This command acts as a shortcut. When you type it, your Mac automatically looks inside the glider_playground folder, finds the cli.py file, and executes the main() function to start the Uvicorn server.

## Uninstalling

If you ever need to remove the application and its terminal command from your environment, simply run:

```bash pip uninstall glider-playground``` 