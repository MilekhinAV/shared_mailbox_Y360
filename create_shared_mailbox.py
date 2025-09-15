#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Создаёт общий ящик в Яндекс 360 и назначает доступы из actors.csv.
- TOKEN, ORG_ID, (опц.) NOTIFY берутся из .env
- Спрашивает email/name/description у пользователя
- Создаёт общий ящик: PUT /admin/v1/org/{ORG_ID}/mailboxes/shared
- Читает actors.csv формата:
    actorId,role[,role2,...[,notify]]
  где notify опционален и может быть all|delegates|none
- Назначает роли:
  POST /admin/v1/org/{ORG_ID}/mailboxes/set/{MAILBOX_ID}?actorId=...&notify=...
"""

import os
import sys
import json
from typing import List, Tuple, Optional
import requests
from dotenv import load_dotenv

VALID_NOTIFY = {"all", "delegates", "none"}

def die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)

def ask(prompt: str, default: Optional[str] = None) -> str:
    try:
        s = input(prompt).strip()
        if not s and default is not None:
            return default
        return s
    except KeyboardInterrupt:
        die("\nПрервано пользователем")

def load_env() -> tuple[str, str, Optional[str]]:
    load_dotenv()
    token = os.getenv("TOKEN", "").strip()
    org_id = os.getenv("ORG_ID", "").strip()
    notify = os.getenv("NOTIFY", "").strip().lower() or None
    if not token or not org_id:
        die("Ошибка: в .env должны быть заданы TOKEN и ORG_ID")
    if notify and notify not in VALID_NOTIFY:
        print(f"⚠ Значение NOTIFY='{notify}' в .env некорректно. Игнорирую, спрошу при запуске.")
        notify = None
    return token, org_id, notify

def json_get_any(d: dict, keys: List[str], default=None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default

def parse_actors_csv(path: str) -> List[Tuple[str, List[str], Optional[str]]]:
    if not os.path.isfile(path):
        die(f"Файл не найден: {path}")
    out: List[Tuple[str, List[str], Optional[str]]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue
            parts = [p.strip() for p in raw.split(",")]
            parts = [p for p in parts if p]  # убираем пустые
            if len(parts) < 2:
                print(f"⚠ Пропуск строки {line_no}: нужно минимум 2 колонки (actorId, role[,...])")
                continue
            actor_id = parts[0]
            payload = parts[1:]
            per_row_notify: Optional[str] = None
            if payload and payload[-1].lower() in VALID_NOTIFY:
                per_row_notify = payload[-1].lower()
                roles = payload[:-1]
            else:
                roles = payload
            if not roles:
                print(f"⚠ Пропуск строки {line_no}: роли не заданы")
                continue
            roles = dedup(roles)
            out.append((actor_id, roles, per_row_notify))
    if not out:
        die("actors.csv пуст или не содержит валидных строк")
    return out

def dedup(items: List[str]) -> List[str]:
    seen, res = set(), []
    for x in items:
        if x not in seen:
            seen.add(x)
            res.append(x)
    return res

def ensure_roles_valid(roles: List[str]) -> List[str]:
    roles = dedup(roles)
    if "shared_mailbox_owner" in roles:
        return ["shared_mailbox_owner"]
    if not any(r in roles for r in ("shared_mailbox_sender", "shared_mailbox_owner")):
        print("⚠ Добавляю 'shared_mailbox_sender' (не указан sender/owner).")
        roles.append("shared_mailbox_sender")
    return roles

class Api360:
    def __init__(self, token: str, org_id: str):
        self.org_id = org_id
        self.base = f"https://api360.yandex.net/admin/v1/org/{org_id}"
        self.hdrs = {
            "Authorization": f"OAuth {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def create_shared_mailbox(self, email: str, name: str, description: str) -> str:
        url = f"{self.base}/mailboxes/shared"
        body = {"email": email, "name": name, "description": description}
        r = requests.put(url, headers=self.hdrs, json=body, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"Ошибка создания общего ящика: {r.status_code} {r.text}")
        data = r.json() if r.content else {}
        mailbox_id = json_get_any(data, ["id", "mailboxId", "resourceId"])
        if not mailbox_id:
            raise RuntimeError(f"Не удалось определить ID созданного ящика из ответа: {json.dumps(data, ensure_ascii=False)}")
        return str(mailbox_id)

    def set_access(self, mailbox_id: str, actor_id: str, roles: List[str], notify: str) -> None:
        roles = ensure_roles_valid(roles)
        url = f"{self.base}/mailboxes/set/{mailbox_id}"
        params = {"actorId": actor_id, "notify": notify}
        body = {"roles": roles}
        r = requests.post(url, headers=self.hdrs, params=params, json=body, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"{actor_id}: ошибка назначения ролей {roles} (notify={notify}) -> {r.status_code} {r.text}")

def resolve_global_notify(env_notify: Optional[str]) -> str:
    if env_notify:
        return env_notify
    print("Укажите глобальное значение notify для назначений прав.")
    print("Возможные значения: all — владельцу и сотруднику; delegates — только сотруднику; none — никому.")
    while True:
        v = ask("notify [all|delegates|none] (по умолчанию all): ", default="all").lower()
        if v in VALID_NOTIFY:
            return v
        print("Некорректное значение. Повторите.")

def main():
    token, org_id, env_notify = load_env()
    api = Api360(token, org_id)

    print("\n=== Создание общего ящика ===")
    email = ask("Укажите email общего ящика (в домене организации): ")
    name = ask("Укажите имя общего ящика (name): ")
    description = ask("Задайте описание (description): ")

    if "@" not in email:
        die("Email выглядит некорректно (нет '@'). Прерываю.")
    if not name:
        die("Имя (name) не может быть пустым.")

    mailbox_id = api.create_shared_mailbox(email=email, name=name, description=description)
    print(f"✓ Общий ящик создан. ID: {mailbox_id}")

    print("\n=== Назначение прав ===")
    actors_path = ask("Путь к файлу actors.csv (Enter — ./actors.csv): ", default="actors.csv")
    entries = parse_actors_csv(actors_path)
    global_notify = resolve_global_notify(env_notify)

    ok, fail = 0, 0
    for actor_id, roles, per_row_notify in entries:
        notify = per_row_notify or global_notify
        try:
            api.set_access(mailbox_id=mailbox_id, actor_id=actor_id, roles=roles, notify=notify)
            print(f"  ✓ {actor_id} -> roles=[{', '.join(ensure_roles_valid(roles))}] notify={notify}")
            ok += 1
        except Exception as e:
            print(f"  ✗ {actor_id} -> {e}")
            fail += 1

    print("\n=== Итоги ===")
    print(f"Успешно: {ok}, ошибок: {fail}")
    print("Проверьте готовность в панели администратора организации (Почта → Общие ящики и доступы).")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        die("\nПрервано пользователем")
