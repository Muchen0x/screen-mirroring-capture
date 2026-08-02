"""Entry point for PyInstaller — launch the GUI."""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from screen_mirroring_capture.gui import run
run()
