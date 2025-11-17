#!/bin/bash

echo "-------------------------------------------"
echo " Installing dependencies for Chat Terminal "
echo "-------------------------------------------"

# Check python
if command -v python3 &>/dev/null; then
    PY=python3
elif command -v python &>/dev/null; then
    PY=python
else
    echo "Python not found. Install Python 3 first."
    exit 1
fi

# Upgrade pip
$PY -m pip install --upgrade pip

# Install requirements
$PY -m pip install -r requirements.txt

echo "-------------------------------------------"
echo " Installation complete. You can now run:   "
echo "          python3 client.py                "
echo "-------------------------------------------"
