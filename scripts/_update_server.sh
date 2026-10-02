#!/bin/bash
set -e
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
cd /opt/omnialpha
git pull -q origin master
echo "PULLED: $(git log --oneline -1)"
uv pip install -q --python .venv/bin/python -r requirements.txt
echo "DEPS_OK"
.venv/bin/python -m unittest discover -s tests 2>&1 | grep -E 'Ran |OK|FAILED' | tail -3
echo "=== UPDATE_DONE ==="
