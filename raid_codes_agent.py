#!/usr/bin/env python3
"""
RAID: Shadow Legends promo code watcher.

Обходит несколько сайтов-агрегаторов, вытаскивает промокоды,
сравнивает с прошлым запуском и сообщает только о НОВЫХ.

Запуск:
    python raid_codes_agent.py              # обычный прогон
    python raid_codes_agent.py --all        # показать все известные коды
    python raid_codes_agent.py --reset      # забыть историю
"""

import argparse
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ----------------------------------------------------------------------------
# Настройки
# ----------------------------------------------------------------------------

GAMES = {
    "raid": {
        "title": "RAID: Shadow Legends",
        "state": "seen_codes.json",
        "output": "codes.md",
        "note": "Ввод: Меню → Промокоды. Один код в 24 часа.",
        "sources": [
            "https://www.pockettactics.com/raid/promo-codes",
            "https://www.pcgamesn.com/raid-shadow-legends/codes",
            "https://www.dexerto.com/gaming/raid-shadow-legends-promo-codes-free-silver-xp-boosts-1773448/",
            "https://www.pocketgamer.com/raid-shadow-legends/redeem-codes/",
            "https://mmoculture.com/2026/08/raid-shadow-legends-codes/",
        ],
        "rewards": (
            r"\b(silver|energy|brew|brews|shard|shards|tome|tomes|multi-?battle|"
            r"champion|xp|gem|gems|chicken|refill|instant battle|epic|legendary|rare)\b"
        ),
    },
    "ludus": {
        "title": "LUDUS: Merge Arena PvP",
        "state": "seen_codes_ludus.json",
        "output": "codes_ludus.md",
        "note": "Ввод: Меню (☰) → Promo Code. Коды можно вводить подряд, один раз каждый.",
        "sources": [
            "https://www.pocketgamer.com/ludus-merge-battle-arena/codes/",
            "https://www.dudcode.com/code/ludus-codes/",
            "https://ponly.com/ludus-promo-codes/",
            "https://progamepilot.com/ludus-codes/",
            "https://gamingonphone.com/guides/ludus-promo-codes-and-how-to-use-them/",
            "https://frvr.com/blog/ludus-promo-codes-links/",
        ],
        "rewards": (
            r"\b(gold|emerald|emeralds|compass|compasses|rune|runes|card|cards|"
            r"crystal|crystals|gem|gems|cannonball|cannonballs|hero|heroes|"
            r"legendary|epic|rare|royal|chest|chests|coin|coins)\b"
        ),
    },
}

STATE_FILE = Path(os.getenv("RAID_STATE_FILE", "seen_codes.json"))
OUTPUT_FILE = Path(os.getenv("RAID_OUTPUT_FILE", "codes.md"))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Слова-маркеры награды. Строка-кандидат должна содержать хотя бы одно —
# это главный фильтр, который отсекает случайный текст.
REWARD_WORDS = re.compile(r"$^")  # задаётся в main() по выбранной игре

# Мусор, который часто выглядит как код, но им не является.
STOPWORDS = {
    "raid", "codes", "code", "shadow", "legends", "promo", "plarium", "new",
    "player", "players", "active", "expired", "working", "rewards", "reward",
    "silver", "energy", "brews", "brew", "shards", "shard", "tome", "tomes",
    "champion", "champions", "free", "how", "redeem", "january", "february",
    "march", "april", "may", "june", "july", "august", "september", "october",
    "november", "december", "update", "updated", "guide", "list", "tier",
    "android", "ios", "pc", "mac", "expires", "expiry", "note", "click",
    "here", "read", "more", "the", "and", "for", "you", "all", "faq",
    "tierlist", "tier", "gameplay", "download", "account", "reddit",
    "discord", "twitter", "youtube", "facebook", "subscribe", "newsletter",
    "advertisement", "related", "comments", "share", "home", "about",
}

CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{2,24}$")
SPLITTERS = re.compile(r"\s*(?:[-–—•]|:|\u2013|\u2014|\||=>|→)\s+")


# ----------------------------------------------------------------------------
# Извлечение кодов
# ----------------------------------------------------------------------------

def looks_like_code(token: str, strong_position: bool = False,
                    reward_hits: int = 0) -> bool:
    """Похож ли токен на промокод.

    strong_position — токен стоит первым в строке вида "КОД — награды".
    reward_hits — сколько слов-наград найдено в этой строке.
    """
    token = token.strip().strip('"\u201c\u201d\u2018\u2019*.,()[]')
    if not CODE_RE.match(token):
        return False
    if token.lower() in STOPWORDS:
        return False
    if token.isdigit():
        return False
    # Код почти всегда либо ВЕСЬ КАПСОМ, либо с цифрой, либо camelCase.
    has_digit = any(c.isdigit() for c in token)
    all_caps = token.isupper() and len(token) >= 3
    inner_caps = any(c.isupper() for c in token[1:]) and not token.isupper()
    if has_digit or all_caps or inner_caps:
        return True
    # Коды типа "midgamejoke" — сплошь строчные. Принимаем только если
    # токен стоит на месте кода и строка явно про награды.
    return strong_position and len(token) >= 5 and reward_hits >= 2


def extract_from_html(html: str) -> dict:
    """Возвращает {код: строка-контекст}."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()

    found = {}

    # 1. Явная разметка: <code>, <strong>, <b> внутри строк с наградами
    for el in soup.find_all(["li", "td", "p"]):
        line = " ".join(el.get_text(" ", strip=True).split())
        if not line or len(line) > 400:
            continue
        hits = len(set(m.group(0).lower() for m in REWARD_WORDS.finditer(line)))
        if hits == 0:
            continue

        candidates = []

        # токены в жирном/моноширинном внутри этой строки
        for inner in el.find_all(["code", "strong", "b", "mark", "span"]):
            t = inner.get_text(" ", strip=True)
            if t and " " not in t:
                candidates.append((t, False))

        # первый токен до разделителя ("КОД — награды")
        parts = SPLITTERS.split(line, maxsplit=1)
        if len(parts) == 2 and " " not in parts[0].strip():
            candidates.append((parts[0].strip(), True))

        for c, strong in candidates:
            if looks_like_code(c, strong_position=strong, reward_hits=hits):
                found.setdefault(c.strip('"\u201c\u201d*.,()[]'), line[:220])

    # 2. Списки кодов подряд: "AAA, BBB, CCC" или "AAA | BBB | CCC".
    #    Награды рядом может не быть, поэтому требуем минимум 3 похожих токена.
    for el in soup.find_all(["li", "p", "td", "div"]):
        line = " ".join(el.get_text(" ", strip=True).split())
        if not line or len(line) > 2000 or ("," not in line and "|" not in line):
            continue
        tokens = [t.strip(" .;\u00b7") for t in re.split(r"[,|\u00b7]", line)]
        strict = [
            t for t in tokens
            if CODE_RE.match(t) and len(t) >= 6 and t.lower() not in STOPWORDS
            and t.upper() == t and not t.isdigit()
        ]
        if len(strict) >= 3 and len(strict) >= len(tokens) * 0.6:
            for t in strict:
                found.setdefault(t, f"из списка кодов ({len(strict)} шт.)")

    # 3. Таблицы: первая ячейка = код, вторая = награда
    for row in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
        if len(cells) >= 2 and cells[0] and " " not in cells[0]:
            if looks_like_code(cells[0]) and REWARD_WORDS.search(" ".join(cells[1:])):
                found.setdefault(cells[0], " — ".join(cells[:2])[:220])

    return found


def fetch(url: str, timeout: int = 25) -> str | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return r.text
    except Exception as e:  # noqa: BLE001
        print(f"  [!] {url}: {e}", file=sys.stderr)
        return None


def scan(sources: list[str]) -> dict:
    """{код: {'context': str, 'sources': [url]}}"""
    result: dict[str, dict] = {}
    for url in sources:
        print(f"  → {url}")
        html = fetch(url)
        if not html:
            continue
        for code, ctx in extract_from_html(html).items():
            entry = result.setdefault(code, {"context": ctx, "sources": []})
            if url not in entry["sources"]:
                entry["sources"].append(url)
        time.sleep(1.5)  # вежливая пауза
    return result


# ----------------------------------------------------------------------------
# Состояние
# ----------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("  [!] Файл состояния повреждён, начинаю заново.", file=sys.stderr)
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


# ----------------------------------------------------------------------------
# Уведомления
# ----------------------------------------------------------------------------

def notify_telegram(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=20,
        ).raise_for_status()
        print("  ✓ Отправлено в Telegram")
    except Exception as e:  # noqa: BLE001
        print(f"  [!] Telegram: {e}", file=sys.stderr)


def notify_email(subject: str, body: str) -> None:
    host = os.getenv("SMTP_HOST")
    to_addr = os.getenv("EMAIL_TO")
    if not (host and to_addr):
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.getenv("EMAIL_FROM", os.getenv("SMTP_USER", to_addr))
    msg["To"] = to_addr
    msg.set_content(body)
    try:
        with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=25) as s:
            s.starttls()
            if os.getenv("SMTP_USER"):
                s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASS", ""))
            s.send_message(msg)
        print("  ✓ Отправлено на почту")
    except Exception as e:  # noqa: BLE001
        print(f"  [!] Email: {e}", file=sys.stderr)


# ----------------------------------------------------------------------------
# Отчёт
# ----------------------------------------------------------------------------

def write_report(state: dict, title: str, note: str) -> None:
    lines = [
        f"# Промокоды: {title}",
        "",
        f"Обновлено: {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC · "
        f"всего кодов: {len(state)}",
        "",
        "| Код | Что даёт | Впервые замечен |",
        "| --- | --- | --- |",
    ]
    ordered = sorted(state.items(), key=lambda kv: kv[1]["first_seen"], reverse=True)
    for code, info in ordered:
        ctx = info["context"].replace("|", "/")[:150]
        lines.append(f"| `{code}` | {ctx} | {info['first_seen'][:10]} |")
    lines += ["", note]
    OUTPUT_FILE.write_text("\n".join(lines), encoding="utf-8")


# ----------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Агент поиска промокодов")
    ap.add_argument("--game", choices=sorted(GAMES), default="raid",
                    help="какая игра (по умолчанию raid)")
    ap.add_argument("--all", action="store_true", help="показать все известные коды")
    ap.add_argument("--reset", action="store_true", help="очистить историю")
    ap.add_argument("--test", action="store_true",
                    help="отправить тестовое сообщение и выйти")
    args = ap.parse_args()

    global REWARD_WORDS, STATE_FILE, OUTPUT_FILE
    game = GAMES[args.game]
    REWARD_WORDS = re.compile(game["rewards"], re.I)
    STATE_FILE = Path(os.getenv("RAID_STATE_FILE", game["state"]))
    OUTPUT_FILE = Path(os.getenv("RAID_OUTPUT_FILE", game["output"]))

    if args.test:
        msg = ("✅ Проверка связи.\n\n"
               f"Агент промокодов ({game['title']}) подключён к этому чату.\n"
               "Дальше сообщения будут приходить только при появлении новых кодов.")
        print(msg)
        if not os.getenv("TELEGRAM_BOT_TOKEN"):
            print("\n[!] TELEGRAM_BOT_TOKEN не задан — отправлять некуда.",
                  file=sys.stderr)
            return 1
        if not os.getenv("TELEGRAM_CHAT_ID"):
            print("\n[!] TELEGRAM_CHAT_ID не задан — отправлять некуда.",
                  file=sys.stderr)
            return 1
        notify_telegram(msg)
        notify_email("Проверка связи: агент промокодов RAID", msg)
        return 0

    if args.reset and STATE_FILE.exists():
        STATE_FILE.unlink()
        print("История очищена.")

    state = load_state()
    first_run = not state

    print(f"[{game['title']}] проверяю {len(game['sources'])} источников…")
    found = scan(game["sources"])
    print(f"Найдено кандидатов: {len(found)}")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new_codes = []
    for code, info in found.items():
        if code in state:
            state[code]["last_seen"] = now
            for s in info["sources"]:
                if s not in state[code]["sources"]:
                    state[code]["sources"].append(s)
        else:
            state[code] = {
                "context": info["context"],
                "sources": info["sources"],
                "first_seen": now,
                "last_seen": now,
            }
            new_codes.append(code)

    save_state(state)
    write_report(state, game["title"], game["note"])

    if args.all:
        for code, info in sorted(state.items()):
            print(f"{code:<24} {info['context'][:90]}")
        return 0

    if first_run:
        print(f"\nПервый запуск: сохранил {len(new_codes)} кодов как базу.")
        print(f"Список: {OUTPUT_FILE}")
        return 0

    if not new_codes:
        print("\nНовых кодов нет.")
        return 0

    body_lines = [f"🎁 {game['title']} — новых кодов: {len(new_codes)}", ""]
    for code in new_codes:
        body_lines.append(f"• {code} — {state[code]['context'][:140]}")
    body_lines += ["", game["note"]]
    body = "\n".join(body_lines)

    print("\n" + body)
    notify_telegram(body)
    notify_email(f"Новые промокоды: {game['title']} ({len(new_codes)})", body)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
