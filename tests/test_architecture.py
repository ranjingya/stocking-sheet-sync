"""验证领域依赖方向与统一命令入口。"""

import ast
from pathlib import Path

import pytest

from stocking_sheet_sync.entrypoints.cli import COMMANDS, run


def test_domain_has_no_external_or_application_dependencies():
    root = Path(__file__).resolve().parents[1] / "src/stocking_sheet_sync/domain"
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(
                    (
                        "stocking_sheet_sync.infrastructure",
                        "stocking_sheet_sync.services",
                        "stocking_sheet_sync.entrypoints",
                        "stocking_sheet_sync.settings",
                        "httpx",
                        "pymysql",
                        "redis",
                    )
                ), path


@pytest.mark.parametrize("command", COMMANDS)
def test_all_cli_subcommands_load_and_show_help(command, capsys):
    with pytest.raises(SystemExit) as error:
        run([command, "--help"])
    assert error.value.code == 0
    assert "usage:" in capsys.readouterr().out
