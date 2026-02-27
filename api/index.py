import sys
import os

# Add the parent directory to sys.path to find app.py
sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from app import app

def handler(request, response):
    return app(request.environ, response.start_response)
