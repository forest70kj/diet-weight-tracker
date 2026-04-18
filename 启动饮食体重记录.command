#!/bin/zsh

cd "$(dirname "$0")"
python3 server.py --host 0.0.0.0 --port 8766 --open-browser
