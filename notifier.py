#!/usr/bin/env python3
"""Анальный куфар — Telegram-бот для мониторинга объявлений Kufar."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


CACHE_FILE_NAME = "cached-data.json"
CONFIGURATION_FILE_NAME = "kufar-configuration.json"
KUFAR_BASE_URL = "https://searchapi.kufar.by/v1/search/rendered-paginated"
DEFAULT_MAX_PRICE = "1000000000"
MAX_IMAGES_IN_GROUP = 10
PLACEHOLDER_PHOTO_URL = "https://via.placeholder.com/1080"

REGIONS = {
    1: "Брест",
    2: "Гомель",
    3: "Гродно",
    4: "Могилёв",
    5: "Минская обл.",
    6: "Витебск",
    7: "Минск",
}


@dataclass
class Advert:
    title: str
    ad_id: int
    date: datetime | None
    price_byn_cents: int
    seller_name: str
    phone_number_is_visible: bool
    link: str
    tag: str | None = None
    images: list[str] = field(default_factory=list)


def load_json(path: Path) -> Any:
    print(f'[Загрузка файла]: "{path}"', flush=True)
    if not path.exists():
        raise SystemExit("[ОШИБКА]: Файл не существует по данному пути или к нему нет доступа.")
    if path.stat().st_size > 4_000_000:
        raise SystemExit("[ОШИБКА]: Размер файла превышает 4МБ.")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SystemExit(f'[ОШИБКА]: Невозможно получить данные из файла "{path}"\n::: {exc} :::') from exc


def save_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")


def http_json(url: str, data: dict[str, Any] | None = None, timeout: int = 10) -> Any:
    request_data = None
    headers = {"User-Agent": "Kufar-Telegram-Notifier-Python/1.0"}
    if data is not None:
        request_data = urllib.parse.urlencode(
            {key: value if isinstance(value, str) else json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value) for key, value in data.items()}
        ).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"

    request = urllib.request.Request(url, data=request_data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {details}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Network error: {exc}") from exc

    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body


def joined_price(price: dict[str, Any] | None) -> str | None:
    if not price:
        return None

    price_min = price.get("min")
    price_max = price.get("max")
    if price_min is None and price_max is None:
        return None

    min_value = 0 if price_min is None else int(price_min) * 100
    max_value = DEFAULT_MAX_PRICE if price_max is None else str(int(price_max) * 100)
    return f"r:{min_value},{max_value}"


def add_if_present(params: dict[str, str], name: str, value: Any) -> None:
    if value is not None:
        params[name] = str(value)


def add_if_true(params: dict[str, str], name: str, value: Any) -> None:
    if value is True:
        params[name] = "1"


def build_kufar_params(query: dict[str, Any]) -> dict[str, str]:
    params: dict[str, str] = {}

    add_if_present(params, "query", query.get("tag"))
    add_if_present(params, "lang", query.get("language"))
    add_if_present(params, "size", query.get("limit"))
    add_if_present(params, "prc", joined_price(query.get("price")))
    add_if_present(params, "cur", query.get("currency"))
    add_if_present(params, "cat", query.get("sub-category"))
    add_if_present(params, "prn", query.get("category"))

    add_if_true(params, "ot", query.get("only-title-search"))
    add_if_true(params, "dle", query.get("kufar-delivery-required"))
    add_if_true(params, "sde", query.get("kufar-payment-required"))
    add_if_true(params, "hlv", query.get("kufar-halva-required"))
    add_if_true(params, "oph", query.get("only-with-photos"))
    add_if_true(params, "ovi", query.get("only-with-videos"))
    add_if_true(params, "pse", query.get("only-with-exchange-available"))

    sort_type = query.get("sort-type")
    if sort_type == 1:
        params["sort"] = "prc.d"
    elif sort_type == 2:
        params["sort"] = "prc.a"

    add_if_present(params, "cnd", query.get("condition"))
    if query.get("seller-type"):
        params["cmp"] = "1"
    add_if_present(params, "rgn", query.get("region"))

    areas = query.get("areas")
    if areas:
        params["ar"] = "v.or:" + ",".join(str(area) for area in areas)

    return params


def image_url(image: dict[str, Any]) -> str | None:
    if image.get("yams_storage"):
        image_id = image.get("id")
        if not image_id:
            return None
        return f"https://yams.kufar.by/api/v1/kufar-ads/images/{image_id[:2]}/{image_id}.jpg?rule=pictures"

    media_storage = image.get("media_storage")
    path = image.get("path")
    if media_storage and path:
        return f"https://{media_storage}.kufar.by/v1/gallery/{path}"
    return None


def parse_kufar_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).astimezone(timezone(timedelta(hours=3)))
    except ValueError:
        return None


def parse_price_cents(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def get_seller_name(ad_data: dict[str, Any]) -> str:
    for parameter in ad_data.get("account_parameters", []):
        if parameter.get("p") == "name":
            return str(parameter.get("v", ""))
    return ""


def get_ads(query: dict[str, Any]) -> list[Advert]:
    params = build_kufar_params(query)
    url = f"{KUFAR_BASE_URL}?{urllib.parse.urlencode(params)}"
    response = http_json(url)
    adverts: list[Advert] = []

    for ad_data in response.get("ads", []):
        images = [url for image in ad_data.get("images", []) if (url := image_url(image))]
        adverts.append(
            Advert(
                title=str(ad_data.get("subject", "")),
                ad_id=int(ad_data["ad_id"]),
                date=parse_kufar_date(ad_data.get("list_time")),
                price_byn_cents=parse_price_cents(ad_data.get("price_byn")),
                seller_name=get_seller_name(ad_data),
                phone_number_is_visible=not bool(ad_data.get("phone_hidden", True)),
                link=str(ad_data.get("ad_link", "")),
                tag=query.get("tag"),
                images=images,
            )
        )

    return adverts


def format_advert_date(value: datetime | None) -> str:
    if value is None:
        return "неизвестно"
    return value.strftime("%d.%m.%Y, %H:%M")


def format_advert(ad: Advert) -> str:
    parts: list[str] = []
    if ad.tag:
        parts.append(f"#{ad.tag}")

    parts.extend(
        [
            f"Название: {ad.title}",
            f"Дата: {format_advert_date(ad.date)}",
            f"Цена: {ad.price_byn_cents // 100} BYN",
            "",
            f"Имя продавца: {ad.seller_name}",
            f"Номер телефона не скрыт: {'Да' if ad.phone_number_is_visible else 'Нет'}",
            f"Ссылка: {ad.link}",
        ]
    )
    return "\n".join(parts)


def send_advert(bot_token: str, chat_id: int | str, ad: Advert) -> None:
    caption = format_advert(ad)
    base_url = f"https://api.telegram.org/bot{bot_token}"

    if ad.images:
        media = []
        for index, photo_url in enumerate(ad.images[:MAX_IMAGES_IN_GROUP]):
            item: dict[str, Any] = {"type": "photo", "media": photo_url}
            if index == 0:
                item["caption"] = caption
            media.append(item)
        http_json(f"{base_url}/sendMediaGroup", {"chat_id": chat_id, "media": json.dumps(media, ensure_ascii=False)})
        return

    http_json(
        f"{base_url}/sendPhoto",
        {"chat_id": chat_id, "caption": caption, "photo": PLACEHOLDER_PHOTO_URL},
    )


def mask_token(token: str) -> str:
    if len(token) < 12:
        return "***"
    return f"{token[:8]}...{token[-4:]}"


def format_query_line(index: int, query: dict[str, Any]) -> str:
    tag = query.get("tag") or "[без слова]"
    region = REGIONS.get(int(query["region"]), str(query["region"])) if query.get("region") is not None else "вся РБ"
    price = query.get("price") or {}
    price_text = ""
    if price.get("min") is not None or price.get("max") is not None:
        price_text = f", цена {price.get('min', 0)}–{price.get('max', '∞')} BYN"
    limit = query.get("limit", "?")
    return f"{index + 1}. {tag} | {region} | до {limit} шт.{price_text}"


def inline_keyboard(rows: list[list[dict[str, str]]]) -> str:
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


def btn(text: str, data: str) -> dict[str, str]:
    return {"text": text, "callback_data": data}


DEFAULT_ACCESS_PASSWORD = "anal"


class App:
    def __init__(self, config_path: Path, cache_path: Path, once: bool = False, dry_run: bool = False) -> None:
        self.config_path = config_path
        self.cache_path = cache_path
        self.once = once
        self.dry_run = dry_run
        self.lock = threading.RLock()
        self.config = load_json(config_path)
        self.viewed_ads: dict[str, set[int]] = {}
        self.update_offset = 0
        self.user_state: dict[int, dict[str, Any]] = {}
        self.stop_event = threading.Event()
        # (chat_key, query_fingerprint) already seeded without notifications in this process
        self.primed_queries: set[tuple[str, str]] = set()
        self.migrate_config()
        self.load_cache()

    @property
    def bot_token(self) -> str:
        return str(self.config["telegram"]["bot-token"])

    @property
    def access_password(self) -> str:
        return str(self.config.get("access-password", DEFAULT_ACCESS_PASSWORD))

    @property
    def api_base(self) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}"

    def migrate_config(self) -> None:
        changed = False
        telegram = self.config.setdefault("telegram", {})
        old_chat_id = telegram.pop("chat-id", None)
        if old_chat_id is not None:
            changed = True

        self.config.setdefault("access-password", DEFAULT_ACCESS_PASSWORD)
        users = self.config.setdefault("users", {})
        legacy_queries = self.config.pop("queries", None)
        if legacy_queries:
            changed = True
            if old_chat_id is not None:
                key = str(old_chat_id)
                user = users.setdefault(key, {"authorized": True, "queries": []})
                if not user.get("queries"):
                    user["queries"] = legacy_queries
                user["authorized"] = True
            # If no old chat-id, drop legacy queries — users will recreate via bot.

        self.config.setdefault("delays", {"query": 10, "loop": 1800})
        if changed:
            self.persist_config()
            print("[CONFIG]: migrated to multi-user format", flush=True)

    def load_cache(self) -> None:
        raw = load_json(self.cache_path)
        if isinstance(raw, list):
            known_users = [key for key in self.config.get("users", {}) if key.isdigit()]
            if len(known_users) == 1:
                self.viewed_ads = {known_users[0]: {int(ad_id) for ad_id in raw}}
            else:
                self.viewed_ads = {"_legacy": {int(ad_id) for ad_id in raw}}
            self.persist_cache()
            return
        if isinstance(raw, dict):
            self.viewed_ads = {str(key): {int(ad_id) for ad_id in value} for key, value in raw.items()}
            # If legacy bucket exists and exactly one real user — merge once.
            if "_legacy" in self.viewed_ads:
                known_users = [key for key in self.config.get("users", {}) if key.isdigit()]
                if len(known_users) == 1:
                    target = known_users[0]
                    self.viewed_ads.setdefault(target, set()).update(self.viewed_ads.pop("_legacy"))
                    self.persist_cache()
            return
        raise SystemExit("[ОШИБКА]: Файл кеша должен быть объектом или массивом.")

    def persist_config(self) -> None:
        with self.lock:
            save_json(self.config_path, self.config)

    def persist_cache(self) -> None:
        with self.lock:
            payload = {key: sorted(ids) for key, ids in self.viewed_ads.items()}
            save_json(self.cache_path, payload)

    def user_key(self, chat_id: int) -> str:
        return str(chat_id)

    @staticmethod
    def query_fingerprint(query: dict[str, Any]) -> str:
        return json.dumps(query, sort_keys=True, ensure_ascii=False)

    def prime_query(self, chat_id: int, query: dict[str, Any]) -> int:
        """Remember current ads without sending notifications."""
        cache_key = self.user_key(chat_id)
        fingerprint = self.query_fingerprint(query)
        try:
            ads = get_ads(query)
        except Exception as exc:
            print(f"[ERROR (prime_query)]: {exc}", file=sys.stderr, flush=True)
            return 0

        added = 0
        with self.lock:
            seen = self.viewed_ads.setdefault(cache_key, set())
            for ad in ads:
                if ad.ad_id not in seen:
                    seen.add(ad.ad_id)
                    added += 1
            self.primed_queries.add((cache_key, fingerprint))

        if added > 0:
            self.persist_cache()

        print(
            f"[PRIME]: chat={chat_id} tag={query.get('tag')!r} "
            f"seeded={len(ads)} new_ids={added}",
            flush=True,
        )
        return added

    def get_user(self, chat_id: int) -> dict[str, Any]:
        users = self.config.setdefault("users", {})
        return users.setdefault(self.user_key(chat_id), {"authorized": False, "queries": []})

    def is_authorized(self, chat_id: int) -> bool:
        with self.lock:
            return bool(self.get_user(chat_id).get("authorized"))

    def set_authorized(self, chat_id: int, value: bool = True) -> None:
        with self.lock:
            user = self.get_user(chat_id)
            user["authorized"] = value
            user.setdefault("queries", [])
            self.persist_config()

    def get_user_queries(self, chat_id: int) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.get_user(chat_id).get("queries", []))

    def get_user_delays(self, chat_id: int) -> dict[str, Any]:
        with self.lock:
            user = self.get_user(chat_id)
            defaults = self.config.get("delays", {})
            delays = user.get("delays") or {}
            return {
                "query": int(delays.get("query", defaults.get("query", 5))),
                "loop": int(delays.get("loop", defaults.get("loop", 30))),
            }

    def tg(self, method: str, payload: dict[str, Any] | None = None, timeout: int = 30) -> Any:
        return http_json(f"{self.api_base}/{method}", payload, timeout=timeout)

    def send_text(self, chat_id: int, text: str, reply_markup: str | None = None) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self.tg("sendMessage", payload)

    def edit_text(self, chat_id: int, message_id: int, text: str, reply_markup: str | None = None) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            self.tg("editMessageText", payload)
        except RuntimeError:
            self.send_text(chat_id, text, reply_markup)

    def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
        self.tg("answerCallbackQuery", payload)

    def main_menu_markup(self) -> str:
        return inline_keyboard(
            [
                [btn("📋 Мои поиски", "menu:list"), btn("➕ Добавить", "menu:add")],
                [btn("⏱ Интервалы", "menu:delays"), btn("ℹ️ Статус", "menu:status")],
                [btn("🚪 Выйти", "menu:logout")],
            ]
        )

    def draft_markup(self) -> str:
        return inline_keyboard(
            [
                [btn("🏙 Регион", "draft:region"), btn("💰 Цена до", "draft:price")],
                [btn("🔢 Лимит", "draft:limit"), btn("✅ Сохранить", "draft:save")],
                [btn("❌ Отмена", "draft:cancel"), btn("🏠 Меню", "menu:home")],
            ]
        )

    def queries_markup(self, chat_id: int) -> str:
        rows: list[list[dict[str, str]]] = []
        for index, query in enumerate(self.get_user_queries(chat_id)):
            tag = str(query.get("tag") or f"#{index + 1}")[:28]
            rows.append([btn(f"🗑 Удалить: {tag}", f"del:{index}")])
        rows.append([btn("🏠 Меню", "menu:home")])
        return inline_keyboard(rows)

    def region_markup(self, prefix: str) -> str:
        rows: list[list[dict[str, str]]] = []
        items = list(REGIONS.items())
        for i in range(0, len(items), 2):
            chunk = items[i : i + 2]
            rows.append([btn(name, f"{prefix}:{code}") for code, name in chunk])
        rows.append([btn("Вся Беларусь", f"{prefix}:0"), btn("⬅️ Назад", "draft:back")])
        return inline_keyboard(rows)

    def price_markup(self) -> str:
        return inline_keyboard(
            [
                [btn("100", "price:100"), btn("300", "price:300"), btn("500", "price:500")],
                [btn("800", "price:800"), btn("1500", "price:1500"), btn("Без лимита", "price:0")],
                [btn("✏️ Ввести число", "price:custom"), btn("⬅️ Назад", "draft:back")],
            ]
        )

    def limit_markup(self) -> str:
        return inline_keyboard(
            [
                [btn("5", "limit:5"), btn("10", "limit:10"), btn("20", "limit:20")],
                [btn("⬅️ Назад", "draft:back")],
            ]
        )

    def delays_markup(self) -> str:
        return inline_keyboard(
            [
                [btn("Цикл 5 мин", "delay:loop:300"), btn("Цикл 15 мин", "delay:loop:900")],
                [btn("Цикл 30 мин", "delay:loop:1800"), btn("Цикл 60 мин", "delay:loop:3600")],
                [btn("🏠 Меню", "menu:home")],
            ]
        )

    def draft_summary(self, draft: dict[str, Any]) -> str:
        region_code = draft.get("region")
        if region_code:
            region = REGIONS.get(int(region_code), str(region_code))
        else:
            region = "вся РБ"
        price = draft.get("price") or {}
        max_price = price.get("max")
        price_text = f"до {max_price} BYN" if max_price is not None else "без лимита"
        return (
            "Черновик поиска:\n"
            f"• Запрос: {draft.get('tag')}\n"
            f"• Регион: {region}\n"
            f"• Цена: {price_text}\n"
            f"• Лимит: {draft.get('limit', 5)}\n"
            f"• Только заголовок: {'да' if draft.get('only-title-search') else 'нет'}\n\n"
            "Настройте кнопками или сохраните."
        )

    def ask_password(self, chat_id: int) -> None:
        self.user_state[chat_id] = {"mode": "await_password"}
        self.send_text(chat_id, "Введите пароль для доступа к боту:")

    def require_auth(self, chat_id: int) -> bool:
        if self.is_authorized(chat_id):
            return True
        self.ask_password(chat_id)
        return False

    def show_home(self, chat_id: int, message_id: int | None = None) -> None:
        text = (
            "Анальный куфар\n\n"
            "Управляйте поисками кнопками ниже.\n"
            "Новые объявления будут приходить в этот чат."
        )
        markup = self.main_menu_markup()
        if message_id is None:
            self.send_text(chat_id, text, markup)
        else:
            self.edit_text(chat_id, message_id, text, markup)

    def show_list(self, chat_id: int, message_id: int | None = None) -> None:
        queries = self.get_user_queries(chat_id)
        if not queries:
            text = "Поисков пока нет.\nНажмите «Добавить»."
        else:
            lines = ["Ваши поиски:\n"]
            lines.extend(format_query_line(i, q) for i, q in enumerate(queries))
            text = "\n".join(lines)
        markup = self.queries_markup(chat_id)
        if message_id is None:
            self.send_text(chat_id, text, markup)
        else:
            self.edit_text(chat_id, message_id, text, markup)

    def show_status(self, chat_id: int, message_id: int | None = None) -> None:
        delays = self.get_user_delays(chat_id)
        key = self.user_key(chat_id)
        cached = len(self.viewed_ads.get(key, set()))
        text = (
            "Статус\n\n"
            f"Поисков: {len(self.get_user_queries(chat_id))}\n"
            f"В кэше объявлений: {cached}\n"
            f"Пауза между запросами: {delays['query']} с\n"
            f"Пауза цикла: {delays['loop']} с"
        )
        markup = inline_keyboard([[btn("🏠 Меню", "menu:home")]])
        if message_id is None:
            self.send_text(chat_id, text, markup)
        else:
            self.edit_text(chat_id, message_id, text, markup)

    def start_add(self, chat_id: int, message_id: int | None = None) -> None:
        self.user_state[chat_id] = {"mode": "await_tag"}
        text = "Пришлите ключевое слово для поиска.\nНапример: велосипед"
        markup = inline_keyboard([[btn("❌ Отмена", "draft:cancel")]])
        if message_id is None:
            self.send_text(chat_id, text, markup)
        else:
            self.edit_text(chat_id, message_id, text, markup)

    def ask_price(self, chat_id: int, message_id: int | None = None) -> None:
        state = self.user_state.setdefault(chat_id, {})
        state["mode"] = "await_price"
        text = (
            "Максимальная цена (BYN)\n\n"
            "Пришлите число сообщением, например: 750\n"
            "Или выберите готовый вариант:"
        )
        if message_id is None:
            self.send_text(chat_id, text, self.price_markup())
        else:
            self.edit_text(chat_id, message_id, text, self.price_markup())

    def ask_custom_price(self, chat_id: int, message_id: int | None = None) -> None:
        state = self.user_state.setdefault(chat_id, {})
        state["mode"] = "await_price"
        text = "Введите максимальную цену числом в BYN.\nНапример: 750"
        markup = inline_keyboard([[btn("Без лимита", "price:0"), btn("⬅️ Назад", "draft:back")]])
        if message_id is None:
            self.send_text(chat_id, text, markup)
        else:
            self.edit_text(chat_id, message_id, text, markup)

    def apply_max_price(self, chat_id: int, value: int, message_id: int | None = None) -> None:
        state = self.user_state.setdefault(chat_id, {})
        draft = state.setdefault("draft", {})
        state["mode"] = "edit_draft"
        if value <= 0:
            draft.pop("price", None)
        else:
            draft["price"] = {"min": 0, "max": value}
        text = self.draft_summary(draft)
        if message_id is None:
            self.send_text(chat_id, text, self.draft_markup())
        else:
            self.edit_text(chat_id, message_id, text, self.draft_markup())

    def handle_callback(self, callback: dict[str, Any]) -> None:
        callback_id = callback["id"]
        data = callback.get("data") or ""
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = int(chat.get("id", 0))
        message_id = int(message.get("message_id", 0))

        if not self.is_authorized(chat_id):
            self.answer_callback(callback_id, "Нужен пароль")
            self.ask_password(chat_id)
            return

        self.answer_callback(callback_id)

        if data == "menu:home":
            self.user_state.pop(chat_id, None)
            self.show_home(chat_id, message_id)
            return
        if data == "menu:list":
            self.show_list(chat_id, message_id)
            return
        if data == "menu:add":
            self.start_add(chat_id, message_id)
            return
        if data == "menu:status":
            self.show_status(chat_id, message_id)
            return
        if data == "menu:logout":
            self.set_authorized(chat_id, False)
            self.user_state.pop(chat_id, None)
            self.edit_text(chat_id, message_id, "Вы вышли. Чтобы снова войти — /start и пароль.")
            return
        if data == "menu:delays":
            delays = self.get_user_delays(chat_id)
            text = f"Интервал полного цикла сейчас: {delays['loop']} с\nВыберите новый:"
            self.edit_text(chat_id, message_id, text, self.delays_markup())
            return

        if data.startswith("del:"):
            index = int(data.split(":", 1)[1])
            with self.lock:
                queries = self.get_user(chat_id).setdefault("queries", [])
                if 0 <= index < len(queries):
                    removed = queries.pop(index)
                    self.persist_config()
                    print(f"[BOT]: removed query {removed.get('tag')} for {chat_id}", flush=True)
            self.show_list(chat_id, message_id)
            return

        if data.startswith("delay:loop:"):
            seconds = int(data.split(":")[-1])
            with self.lock:
                user = self.get_user(chat_id)
                delays = user.setdefault("delays", dict(self.config.get("delays", {})))
                delays["loop"] = seconds
                delays.setdefault("query", self.config.get("delays", {}).get("query", 10))
                self.persist_config()
            self.edit_text(
                chat_id,
                message_id,
                f"Цикл обновлён: {seconds} с",
                inline_keyboard([[btn("🏠 Меню", "menu:home")]]),
            )
            return

        if data == "draft:cancel":
            self.user_state.pop(chat_id, None)
            self.show_home(chat_id, message_id)
            return

        if data == "draft:back":
            state = self.user_state.get(chat_id) or {}
            draft = state.get("draft")
            if not draft:
                self.show_home(chat_id, message_id)
                return
            state["mode"] = "edit_draft"
            self.edit_text(chat_id, message_id, self.draft_summary(draft), self.draft_markup())
            return

        if data == "draft:region":
            self.edit_text(chat_id, message_id, "Выберите регион:", self.region_markup("region"))
            return
        if data == "draft:price":
            self.ask_price(chat_id, message_id)
            return
        if data == "draft:limit":
            state = self.user_state.setdefault(chat_id, {})
            state["mode"] = "edit_draft"
            self.edit_text(chat_id, message_id, "Сколько объявлений брать за раз:", self.limit_markup())
            return

        if data == "draft:save":
            state = self.user_state.get(chat_id) or {}
            draft = state.get("draft")
            if not draft or not draft.get("tag"):
                self.edit_text(chat_id, message_id, "Черновик пуст. Добавьте поиск заново.", self.main_menu_markup())
                return
            with self.lock:
                self.get_user(chat_id).setdefault("queries", []).append(draft)
                self.persist_config()
            self.user_state.pop(chat_id, None)
            self.prime_query(chat_id, draft)
            self.edit_text(
                chat_id,
                message_id,
                f"Сохранено: {draft['tag']}\nТекущие объявления пропущены — пришлю только новые.",
                inline_keyboard([[btn("📋 К списку", "menu:list"), btn("🏠 Меню", "menu:home")]]),
            )
            return

        if data.startswith("region:"):
            code = int(data.split(":", 1)[1])
            state = self.user_state.setdefault(chat_id, {})
            draft = state.setdefault("draft", {})
            state["mode"] = "edit_draft"
            if code == 0:
                draft.pop("region", None)
                draft.pop("areas", None)
            else:
                draft["region"] = code
            self.edit_text(chat_id, message_id, self.draft_summary(draft), self.draft_markup())
            return

        if data == "price:custom":
            self.ask_custom_price(chat_id, message_id)
            return

        if data.startswith("price:"):
            value = int(data.split(":", 1)[1])
            self.apply_max_price(chat_id, value, message_id)
            return

        if data.startswith("limit:"):
            value = int(data.split(":", 1)[1])
            state = self.user_state.setdefault(chat_id, {})
            draft = state.setdefault("draft", {})
            draft["limit"] = value
            self.edit_text(chat_id, message_id, self.draft_summary(draft), self.draft_markup())
            return

    def handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        chat_id = int(chat.get("id", 0))
        text = (message.get("text") or "").strip()
        if not text:
            return

        state = self.user_state.get(chat_id) or {}

        if text.startswith("/start"):
            if self.is_authorized(chat_id):
                self.user_state.pop(chat_id, None)
                self.show_home(chat_id)
            else:
                self.ask_password(chat_id)
            return

        if state.get("mode") == "await_password":
            if text == self.access_password:
                self.set_authorized(chat_id, True)
                self.user_state.pop(chat_id, None)
                self.send_text(chat_id, "Доступ открыт.")
                self.show_home(chat_id)
            else:
                self.send_text(chat_id, "Неверный пароль. Попробуйте ещё раз или /start.")
            return

        if not self.is_authorized(chat_id):
            self.ask_password(chat_id)
            return

        if text.startswith("/menu"):
            self.user_state.pop(chat_id, None)
            self.show_home(chat_id)
            return
        if text.startswith("/list"):
            self.show_list(chat_id)
            return
        if text.startswith("/add"):
            parts = text.split(maxsplit=1)
            if len(parts) == 2:
                self.create_draft(chat_id, parts[1])
            else:
                self.start_add(chat_id)
            return
        if text.startswith("/status"):
            self.show_status(chat_id)
            return
        if text.startswith("/logout"):
            self.set_authorized(chat_id, False)
            self.user_state.pop(chat_id, None)
            self.send_text(chat_id, "Вы вышли. Чтобы снова войти — /start и пароль.")
            return

        if state.get("mode") == "await_tag":
            self.create_draft(chat_id, text)
            return

        if state.get("mode") == "await_price":
            cleaned = text.replace(" ", "").replace(",", "")
            if not cleaned.isdigit():
                self.send_text(
                    chat_id,
                    "Нужно целое число в BYN, например: 750",
                    inline_keyboard([[btn("Без лимита", "price:0"), btn("⬅️ Назад", "draft:back")]]),
                )
                return
            self.apply_max_price(chat_id, int(cleaned))
            return

        self.send_text(
            chat_id,
            "Не понял сообщение. Откройте меню:",
            self.main_menu_markup(),
        )

    def create_draft(self, chat_id: int, tag: str) -> None:
        draft = {
            "tag": tag.strip(),
            "only-title-search": True,
            "limit": 5,
        }
        self.user_state[chat_id] = {"mode": "edit_draft", "draft": draft}
        self.send_text(chat_id, self.draft_summary(draft), self.draft_markup())

    def process_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self.handle_callback(update["callback_query"])
        elif "message" in update:
            self.handle_message(update["message"])

    def bot_loop(self) -> None:
        print("[BOT]: polling Telegram updates...", flush=True)
        while not self.stop_event.is_set():
            try:
                response = self.tg(
                    "getUpdates",
                    {"timeout": 25, "offset": self.update_offset},
                    timeout=35,
                )
                for update in response.get("result", []):
                    self.update_offset = int(update["update_id"]) + 1
                    try:
                        self.process_update(update)
                    except Exception as exc:
                        print(f"[ERROR (bot update)]: {exc}", file=sys.stderr, flush=True)
            except Exception as exc:
                print(f"[ERROR (getUpdates)]: {exc}", file=sys.stderr, flush=True)
                time.sleep(3)

    def watcher_loop(self) -> None:
        print("[WATCHER]: started", flush=True)
        while not self.stop_event.is_set():
            with self.lock:
                users_snapshot = [
                    (int(chat_key), dict(user), list(user.get("queries", [])), dict(user.get("delays") or {}))
                    for chat_key, user in self.config.get("users", {}).items()
                    if user.get("authorized") and user.get("queries")
                ]
                global_delays = dict(self.config.get("delays", {}))
                bot_token = self.bot_token

            next_loop_delay = int(global_delays.get("loop", 30))
            query_delay_default = int(global_delays.get("query", 5))

            for chat_id, _user, queries, user_delays in users_snapshot:
                if self.stop_event.is_set():
                    break
                query_delay = int(user_delays.get("query", query_delay_default))
                loop_delay = int(user_delays.get("loop", next_loop_delay))
                next_loop_delay = min(next_loop_delay, loop_delay)
                cache_key = self.user_key(chat_id)
                sent_count = 0

                for query in queries:
                    if self.stop_event.is_set():
                        break

                    fingerprint = self.query_fingerprint(query)
                    prime_key = (cache_key, fingerprint)
                    if prime_key not in self.primed_queries:
                        # First sight in this process: remember current ads, don't notify.
                        self.prime_query(chat_id, query)
                        self.stop_event.wait(query_delay)
                        continue

                    try:
                        ads = get_ads(query)
                    except Exception as exc:
                        print(f"[ERROR (get_ads)]: {exc}", file=sys.stderr, flush=True)
                        self.stop_event.wait(query_delay)
                        continue

                    for ad in ads:
                        with self.lock:
                            seen = self.viewed_ads.setdefault(cache_key, set())
                            already_seen = ad.ad_id in seen
                            if not already_seen:
                                seen.add(ad.ad_id)
                        if already_seen:
                            continue

                        print(
                            f"[New]: chat={chat_id} [Title: {ad.title}], [ID: {ad.ad_id}], [Tag: {ad.tag}]",
                            flush=True,
                        )
                        sent_count += 1
                        if self.dry_run:
                            print("[DRY-RUN]: Telegram отправка пропущена.", flush=True)
                        else:
                            try:
                                send_advert(bot_token, chat_id, ad)
                            except Exception as exc:
                                print(f"[ERROR (send_advert)]: {exc}", file=sys.stderr, flush=True)
                        time.sleep(0.3)

                    self.stop_event.wait(query_delay)

                if sent_count > 0 and not self.dry_run:
                    self.persist_cache()

            if self.once:
                self.stop_event.set()
                return

            self.stop_event.wait(next_loop_delay)
    def run(self) -> None:
        users = self.config.get("users", {})
        authorized = sum(1 for user in users.values() if user.get("authorized"))
        print("- Telegram:", flush=True)
        print(f"\t- Токен: {mask_token(self.bot_token)}", flush=True)
        print(f"\t- Режим: multi-user, пароль включён", flush=True)
        print(f"- Пользователей: {len(users)} (авторизовано: {authorized})", flush=True)

        if self.once:
            self.watcher_loop()
            return

        bot_thread = threading.Thread(target=self.bot_loop, name="telegram-bot", daemon=True)
        bot_thread.start()
        try:
            self.watcher_loop()
        finally:
            self.stop_event.set()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Анальный куфар — мониторинг объявлений Kufar")
    parser.add_argument("--config", type=Path, default=Path.cwd() / CONFIGURATION_FILE_NAME)
    parser.add_argument("--cache", type=Path, default=Path.cwd() / CACHE_FILE_NAME)
    parser.add_argument("--once", action="store_true", help="Run one pass over configured queries and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Do not send Telegram messages.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    App(args.config, args.cache, args.once, args.dry_run).run()


if __name__ == "__main__":
    main()
