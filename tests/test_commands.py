from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.bot import WiaBot, register_commands  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.service import ShopService  # noqa: E402


def test_command_registration(tmp_path):
    settings = replace(load_settings(), owner_ids=frozenset({1}), guild_id=None)
    db = Database(tmp_path / "commands.db")
    service = ShopService(db, settings, bot=None)
    bot = WiaBot(db, settings, service)
    register_commands(bot)

    names = {command.name for command in bot.tree.get_commands()}
    expected = {"패널", "관리", "등록", "웹패널", "제품전송", "충전", "잔액", "입금", "구매", "공지", "플랜", "라이센스"}
    assert expected.issubset(names), expected - names

    license_group = next(command for command in bot.tree.get_commands() if command.name == "라이센스")
    subcommands = {command.name for command in license_group.commands}
    assert {"발급", "목록", "폐기", "정보"} == subcommands

    settings_group = next(command for command in bot.tree.get_commands() if command.name == "설정")
    setting_commands = {command.name for command in settings_group.commands}
    assert {"보기", "계좌", "입금", "구매수량", "자동충전", "구매", "초기화", "웹"} == setting_commands

    for command in bot.tree.get_commands():
        payload = command.to_dict(bot.tree)
        assert payload["name"]
        assert payload["description"]

    db.close()
