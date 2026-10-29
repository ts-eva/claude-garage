#!/bin/bash
# Double-click in Finder to run the setup wizard.
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null; then
  echo "Python 3 is needed. Install it from https://www.python.org/downloads/ and try again."
  read -r -p "Press Enter to close."; exit 1
fi
python3 code_review_automation.py setup
read -r -p "Press Enter to close."
