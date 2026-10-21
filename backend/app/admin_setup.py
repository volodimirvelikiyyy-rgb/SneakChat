"""Interactive first-run password setup: python -m app.admin_setup"""

from __future__ import annotations

import getpass
import json
import os
import sys
import tempfile

from .admin_auth import auth_file, make_auth_record


def main() -> int:
    destination = auth_file()
    reset = sys.argv[1:] == ["--reset"]
    if destination.exists() and not reset:
        print("Пароль владельца уже настроен. Для смены запустите: python -m app.admin_setup --reset")
        return 1
    if reset and input("Заменить пароль владельца этой локальной админки? Введите ДА: ").strip() != "ДА":
        print("Смена пароля отменена.")
        return 1
    password = getpass.getpass("Новый пароль админки (не менее 12 символов): ")
    if len(password) < 12:
        print("Пароль слишком короткий.")
        return 1
    if password != getpass.getpass("Повторите пароль: "):
        print("Пароли не совпали.")
        return 1
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix="admin-auth-", dir=destination.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(make_auth_record(password), handle)
        os.replace(temp_path, destination)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
    print("Пароль владельца сохранён в профиле Windows. Значение пароля нигде не выводилось.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
