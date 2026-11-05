#!/bin/bash
set -e

cd "$(dirname "$0")"

if [ ! -f ".env" ]; then
    echo "Missing .env file."
    echo "Copy .env.example to .env and add your Riot API key."
    exit 1
fi

if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi

source .venv/bin/activate

python -m pip install --upgrade pip
pip install -r requirements.txt

echo ""
echo "Starting Riftmates..."
echo "Keep this Terminal window open while using the dashboard."
echo ""

python server.py