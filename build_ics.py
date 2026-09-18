#!/usr/bin/env python3
"""Собирает подписной календарь (.ics) с парами группы из РУЗа Финуниверситета.

Запуск:  python build_ics.py            -> создаёт ib24-1.ics
         python build_ics.py --dump     -> дополнительно сохраняет сырой JSON (raw.json)
                                           для отладки, если названия полей отличаются
Зависимостей нет, только стандартная библиотека (Python 3.9+).
"""
import hashlib
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

GROUP = "ИБ24-1"
BASE = "https://ruz.fa.ru/api"
OUT = "ib24-1.ics"
TZ = ZoneInfo("Europe/Moscow")
DAYS_BACK = 30      # сколько дней назад захватывать
DAYS_AHEAD = 150    # сколько дней вперёд (~ семестр, включая сессию)
CHUNK_DAYS = 30     # запрашиваем кусками, чтобы не упереться в лимиты API


def get(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def find_group_id(name):
    data = get(f"{BASE}/search?term={urllib.parse.quote(name)}&type=group")
    for item in data:
        if str(item.get("label", "")).strip().upper() == name.upper():
            return item["id"]
    if data:
        print(f"Точного совпадения нет, беру первое: {data[0].get('label')}", file=sys.stderr)
        return data[0]["id"]
    sys.exit(f"Группа {name} не найдена")


def fetch_lessons(group_id):
    today = datetime.now(TZ).date()
    start = today - timedelta(days=DAYS_BACK)
    end = today + timedelta(days=DAYS_AHEAD)
    lessons, cur = [], start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=CHUNK_DAYS - 1), end)
        url = (
            f"{BASE}/schedule/group/{group_id}"
            f"?start={cur:%Y.%m.%d}&finish={chunk_end:%Y.%m.%d}&lng=1"
        )
        lessons.extend(get(url))
        cur = chunk_end + timedelta(days=1)
    return lessons


def parse_dt(date_str, time_str):
    for fmt in ("%Y.%m.%d %H:%M", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(f"{date_str} {time_str}", fmt).replace(tzinfo=TZ)
        except ValueError:
            continue
    raise ValueError(f"Не разобрал дату/время: {date_str!r} {time_str!r}")


def esc(text):
    return (
        str(text).replace("\\", "\\\\").replace(";", "\\;")
        .replace(",", "\\,").replace("\r", "").replace("\n", "\\n")
    )


def fold(line):
    """Перенос строк по RFC 5545: не больше 75 байт, не режем UTF-8 посередине."""
    b, parts, limit = line.encode("utf-8"), [], 75
    while len(b) > limit:
        cut = limit
        while (b[cut] & 0xC0) == 0x80:
            cut -= 1
        parts.append(b[:cut])
        b, limit = b[cut:], 74
    parts.append(b)
    return "\r\n ".join(p.decode("utf-8") for p in parts)


def utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_event(lesson):
    start = parse_dt(lesson["date"], lesson["beginLesson"])
    end = parse_dt(lesson["date"], lesson["endLesson"])
    subject = lesson.get("discipline") or "Занятие"
    kind = lesson.get("kindOfWork") or ""
    title = f"{subject} ({kind})" if kind else subject

    room = lesson.get("auditorium") or ""
    building = lesson.get("building") or ""
    location = ", ".join(x for x in (room, building) if x)

    teacher = lesson.get("lecturer_title") or lesson.get("lecturer") or ""
    desc_parts = []
    if teacher:
        desc_parts.append(f"Преподаватель: {teacher}")
    if lesson.get("group"):
        desc_parts.append(f"Группа: {lesson['group']}")
    if lesson.get("stream"):
        desc_parts.append(f"Поток: {lesson['stream']}")
    if lesson.get("url1"):
        desc_parts.append(f"Ссылка: {lesson['url1']}")

    uid_src = f"{lesson['date']}|{lesson['beginLesson']}|{subject}|{kind}|{lesson.get('group', '')}"
    uid = hashlib.sha1(uid_src.encode("utf-8")).hexdigest()[:20] + "@ib24-1.ruz"

    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{utc(start)}",  # детерминированный, чтобы файл не менялся без причины
        f"DTSTART:{utc(start)}",
        f"DTEND:{utc(end)}",
        f"SUMMARY:{esc(title)}",
    ]
    if location:
        lines.append(f"LOCATION:{esc(location)}")
    if desc_parts:
        lines.append(f"DESCRIPTION:{esc(chr(10).join(desc_parts))}")
    lines.append("END:VEVENT")
    return uid, lines


def main():
    group_id = find_group_id(GROUP)
    lessons = fetch_lessons(group_id)
    if "--dump" in sys.argv:
        with open("raw.json", "w", encoding="utf-8") as f:
            json.dump(lessons, f, ensure_ascii=False, indent=2)

    events = {}
    for lesson in lessons:
        try:
            uid, lines = build_event(lesson)
        except (KeyError, ValueError) as e:
            print(f"Пропускаю запись ({e}): {lesson}", file=sys.stderr)
            continue
        events[uid] = lines

    if not events:
        sys.exit("Пар не найдено, файл не перезаписываю (проверь API или каникулы)")

    out = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//ib24-1 schedule//RU",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{GROUP}",
        "X-WR-TIMEZONE:Europe/Moscow",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        "X-PUBLISHED-TTL:PT1H",
    ]
    for uid in sorted(events):
        out.extend(events[uid])
    out.append("END:VCALENDAR")

    with open(OUT, "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(fold(l) for l in out) + "\r\n")
    print(f"Готово: {len(events)} пар -> {OUT}")


if __name__ == "__main__":
    main()
