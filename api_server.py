"""Single-process ASGI entrypoint: FastAPI API plus the existing Gradio UI."""

from __future__ import annotations

import gradio as gr

from api.app import create_app
from config import Config
from research_agent import ResearchAgent
from web_ui import build_ui


def create_server():
    cfg = Config.load()
    agent = ResearchAgent(cfg=cfg)
    app = create_app(agent=agent)
    ui = build_ui(cfg=cfg, agent=agent, launch=False)
    return gr.mount_gradio_app(app, ui, path="/")


app = create_server()
