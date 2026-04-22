import subprocess
import os

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts", "read_params.py")


def _write_yaml(tmp_path, content):
    p = tmp_path / "inputs.yaml"
    p.write_text(content)
    return str(p)


def test_read_params_py(tmp_path):
    yaml_content = """\
global:
  monitor_interval_min: 30
  pr_pool_min_threshold: 100
languages:
  py:
    enabled: true
    params:
      timeout: 3200
      cc_timeout: 2400
      n_concurrent: 16
"""
    yaml_path = _write_yaml(tmp_path, yaml_content)
    result = subprocess.run(
        ["python", SCRIPT, "--lang", "py", "--inputs-yaml", yaml_path],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "TIMEOUT=3200" in result.stdout
    assert "CC_TIMEOUT=2400" in result.stdout
    assert "N_CONCURRENT=16" in result.stdout


def test_read_params_rust_defaults(tmp_path):
    yaml_content = """\
global:
  monitor_interval_min: 30
languages:
  rust:
    enabled: true
    params:
      timeout: 4000
      cc_timeout: 3300
      n_concurrent: 20
"""
    yaml_path = _write_yaml(tmp_path, yaml_content)
    result = subprocess.run(
        ["python", SCRIPT, "--lang", "rust", "--inputs-yaml", yaml_path],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "TIMEOUT=4000" in result.stdout
    assert "CC_TIMEOUT=3300" in result.stdout
    assert "N_CONCURRENT=20" in result.stdout


def test_read_params_missing_lang(tmp_path):
    yaml_content = """\
global:
  monitor_interval_min: 30
languages:
  py:
    enabled: true
    params:
      timeout: 3200
      cc_timeout: 2400
      n_concurrent: 16
"""
    yaml_path = _write_yaml(tmp_path, yaml_content)
    result = subprocess.run(
        ["python", SCRIPT, "--lang", "java", "--inputs-yaml", yaml_path],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
