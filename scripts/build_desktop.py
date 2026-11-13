"""Build a Windows installer with its own Python backend and the React assets.

Run with the project's verified Python environment. No .env, runtime data, model
credentials, or user PDFs are included. Model downloads remain on-demand.
"""
from pathlib import Path
import os
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def build_backend():
    sys.path.insert(0, str(ROOT))
    from scripts.fetch_ocr_data import ensure_ocr_data

    tessdata = ensure_ocr_data()
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onedir',
               '--name', 'research-backend', '--distpath', str(ROOT/'build/desktop-backend'),
               '--workpath', str(ROOT/'build/pyinstaller'), '--specpath', str(ROOT/'build'),
               '--paths', str(ROOT), '--add-data', f'{tessdata}{os.pathsep}tessdata']
    for package in ('chromadb', 'langgraph', 'langchain_core', 'onnxruntime', 'tiktoken'):
        command += ['--collect-all', package]
    for package in ('uvicorn', 'tools', 'papers', 'mcp'):
        command += ['--collect-submodules', package]
    for package in ('langgraph', 'langgraph-checkpoint', 'langgraph-prebuilt', 'transformers', 'sentence-transformers'):
        command += ['--copy-metadata', package]
    command += [str(ROOT/'desktop_backend.py')]
    subprocess.run(command, cwd=ROOT, check=True)


def main():
    if sys.platform != 'win32':
        raise SystemExit('Build the Windows desktop distribution on Windows.')
    if '--backend-only' not in sys.argv:
        subprocess.run(['npm.cmd', 'ci'], cwd=ROOT/'frontend', check=True)
        subprocess.run(['npm.cmd', 'run', 'build'], cwd=ROOT/'frontend', check=True)
    build_backend()
    if '--backend-only' not in sys.argv:
        subprocess.run(['npm.cmd', 'ci'], cwd=ROOT/'desktop', check=True)
        subprocess.run(['npm.cmd', 'run', 'dist'], cwd=ROOT/'desktop', check=True,
                       env={**os.environ, 'CSC_IDENTITY_AUTO_DISCOVERY': 'false'})


if __name__ == '__main__':
    main()
