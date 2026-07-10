"""Gunicorn entry point for Render deployment."""

from app import app, load_data

load_data()
