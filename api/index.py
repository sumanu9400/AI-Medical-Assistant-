import sys
import os

# Add the parent directory to sys.path to find app.py
sys.path.append(os.path.dirname(os.path.dirname(__file__)))

try:
    from app import app
    print("Successfully imported app from app.py")
except Exception as e:
    print(f"Error importing app: {e}")
    raise e
