import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/shared/scripts'))
from install_tool import TOOLS, check_tool


def test_browser_cache_directory_or_archive_is_not_a_browser(tmp_path):
    cache = tmp_path / 'node_modules/.remotion/chrome-headless-shell/win64/chrome-headless-shell-win64'
    cache.mkdir(parents=True)
    (cache / 'chrome-headless-shell-win64.zip').write_bytes(b'partial download')
    tool = next(t for t in TOOLS if t.id == 'shell')
    assert check_tool(tool, sys.executable, str(tmp_path))['state'] == 'missing'
    binary = cache / 'chrome-headless-shell.exe'
    binary.write_bytes(b'')
    assert check_tool(tool, sys.executable, str(tmp_path))['state'] == 'missing'
    binary.write_bytes(b'test executable fixture')
    assert check_tool(tool, sys.executable, str(tmp_path))['state'] == 'ok'
