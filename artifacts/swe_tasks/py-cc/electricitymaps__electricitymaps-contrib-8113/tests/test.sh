#!/bin/bash

cd /app/src

# Activate the virtual environment created by poetry
source /app/src/.venv/bin/activate

# Copy HEAD test files from /tests (overwrites BASE state)
mkdir -p "."
cp "/tests/test_parser.py" "test_parser.py"
mkdir -p "tests/config"
cp "/tests/config/__init__.py" "tests/config/__init__.py"
mkdir -p "tests/config"
cp "/tests/config/test_config.py" "tests/config/test_config.py"
mkdir -p "tests/config"
cp "/tests/config/test_config_model.py" "tests/config/test_config_model.py"
mkdir -p "tests/config"
cp "/tests/config/test_config_zones.py" "tests/config/test_config_zones.py"

# Run only the specific test files from the PR
pytest -xvs \
    test_parser.py \
    tests/config/test_config.py \
    tests/config/test_config_model.py \
    tests/config/test_config_zones.py
test_status=$?

if [ $test_status -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
exit "$test_status"

