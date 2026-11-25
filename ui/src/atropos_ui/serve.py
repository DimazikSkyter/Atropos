"""Initialize account replay jobs at process startup, then run the web UI."""
import argparse
import os
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description='Atropos UI with startup replay')
    parser.add_argument('--port', type=int, default=8501)
    parser.add_argument('--host', default='127.0.0.1')
    args = parser.parse_args()
    if os.getenv('ATROPOS_UI_DEMO') != '1':
        from atropos_ui.replay_ui import runtime
        runtime()
    from streamlit.web.cli import main as streamlit_main
    sys.argv = ['streamlit', 'run', str(Path(__file__).resolve().parents[2] / 'app.py'),
                f'--server.address={args.host}', '--server.headless=true', '--server.fileWatcherType=none',
                f'--server.port={args.port}']
    streamlit_main()


if __name__ == '__main__':
    main()
