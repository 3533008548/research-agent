"""ASGI entry point for uvicorn api_server:app."""

from api.server import create_server

app = create_server()
