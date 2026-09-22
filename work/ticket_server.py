"""Local, on-demand reader for the public 12306 search results page."""

from __future__ import annotations

import json
import math
import os
import re
import socket
import threading
import time
import uuid
import webbrowser
import zipfile
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright
from rail_path import Timetable


ROOT = Path(__file__).resolve().parents[1]
if getattr(__import__("sys"), "frozen", False):
    ROOT = Path(__import__("sys")._MEIPASS)
PAGE = ROOT / "outputs" / "12306-live.html"
STATIONS_URL = "https://kyfw.12306.cn/otn/resources/js/framework/station_name.js"
STATION_PATTERN = re.compile(r"@[^|]*\|([^|]+)\|([A-Z]{3})\|")
LOCK = threading.Lock()
LAST_QUERY = 0.0
LAST_SELECTION = 0.0
SELECTION_LOCK = threading.Lock()
CHECKOUT_LOCK = threading.Lock()
STATIONS: dict[str, str] = {}
STATION_CITIES: dict[str, str] = {}
CITY_STATIONS: dict[str, tuple[str, ...]] = {}
ACTIVE_CHECKOUTS: dict[str, dict] = {}
TIMETABLE = None
TIMETABLE_LOCK = threading.Lock()
GTFS_RELEASE_API = "https://api.github.com/repos/wensimehrp/chinese-railway-gtfs/releases/latest"
GTFS_BUNDLED_TAG = "gtfs-20260920-204910"
GTFS_CACHE = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "RailHelper" / "gtfs"
GTFS_META = GTFS_CACHE / "metadata.json"
GTFS_ZIP = GTFS_CACHE / "rail_gtfs.zip"
GTFS_CITIES = ROOT / "work" / "data" / "station_cities.json"


def rail_timetable() -> tuple[Timetable, dict]:
    global TIMETABLE
    with TIMETABLE_LOCK:
        bundled = ROOT / "work" / "data" / "rail_gtfs.zip"
        if not bundled.is_file():
            raise RuntimeError("本地全国时刻表未安装。")
        try:
            metadata = json.loads(GTFS_META.read_text(encoding="utf-8"))
            if not isinstance(metadata, dict):
                raise ValueError("Invalid metadata")
        except (OSError, ValueError):
            metadata = {}
        tag = str(metadata.get("tag", GTFS_BUNDLED_TAG)) if GTFS_ZIP.is_file() else GTFS_BUNDLED_TAG
        warning = ""
        today = date.today().isoformat()
        if metadata.get("checkedOn") != today:
            temp = GTFS_CACHE / "rail_gtfs.download"
            try:
                request = Request(GTFS_RELEASE_API, headers={"User-Agent": "RailHelper/1.0", "Accept": "application/vnd.github+json"})
                with urlopen(request, timeout=10) as response:
                    release = json.load(response)
                latest = str(release["tag_name"])
                if latest != tag or not (GTFS_ZIP.is_file() or latest == GTFS_BUNDLED_TAG):
                    asset = next(item for item in release["assets"] if item["name"] == "output_gtfs.zip")
                    url = str(asset["browser_download_url"])
                    if not re.fullmatch(r"https://github\.com/wensimehrp/chinese-railway-gtfs/releases/download/[^/]+/output_gtfs\.zip", url):
                        raise ValueError("时刻表下载地址无效。")
                    GTFS_CACHE.mkdir(parents=True, exist_ok=True)
                    with urlopen(Request(url, headers={"User-Agent": "RailHelper/1.0"}), timeout=25) as response, temp.open("wb") as target:
                        size = 0
                        while chunk := response.read(65536):
                            size += len(chunk)
                            if size > 30_000_000:
                                raise ValueError("时刻表文件过大。")
                            target.write(chunk)
                    updated = Timetable(temp, GTFS_CITIES)
                    if len(updated.trips) < 1000 or len(updated.names) < 1000:
                        raise ValueError("下载的时刻表内容不完整。")
                    temp.replace(GTFS_ZIP)
                    updated.source = GTFS_ZIP
                    TIMETABLE = updated
                    tag = latest
                GTFS_CACHE.mkdir(parents=True, exist_ok=True)
                new_metadata = {"tag": tag, "checkedOn": today}
                meta_temp = GTFS_CACHE / "metadata.download"
                meta_temp.write_text(json.dumps(new_metadata), encoding="utf-8")
                meta_temp.replace(GTFS_META)
                metadata = new_metadata
            except (OSError, ValueError, KeyError, StopIteration, TypeError, zipfile.BadZipFile) as exc:
                warning = f"今日时刻表更新检查失败，仍使用本地版本：{exc}"
            finally:
                temp.unlink(missing_ok=True)
        source = GTFS_ZIP if GTFS_ZIP.is_file() and tag != GTFS_BUNDLED_TAG else bundled
        if TIMETABLE is None or TIMETABLE.source != source:
            try:
                TIMETABLE = Timetable(source, GTFS_CITIES)
            except (OSError, ValueError, zipfile.BadZipFile) as exc:
                if source == bundled:
                    raise RuntimeError(f"内置时刻表无法读取：{exc}") from exc
                TIMETABLE = Timetable(bundled, GTFS_CITIES)
                tag = GTFS_BUNDLED_TAG
                warning = "本地更新文件损坏，已回退至内置时刻表。"
        return TIMETABLE, {"version": tag.removeprefix("gtfs-"), "checkedOn": metadata.get("checkedOn", ""),
                           "warning": warning}


def station_codes() -> dict[str, str]:
    global STATIONS, STATION_CITIES, CITY_STATIONS
    if not STATIONS:
        with urlopen(STATIONS_URL, timeout=12) as response:
            source = response.read().decode("utf-8-sig")
        records = []
        for raw in source.split("@"):
            fields = raw.split("|")
            if len(fields) < 8 or not re.fullmatch(r"[A-Z]{3}", fields[2]):
                continue
            station, code, city = fields[1].strip(), fields[2], fields[7].strip()
            if station:
                records.append((station, code, city))
        STATIONS = {station: code for station, code, _ in records}
        STATION_CITIES = {station: city for station, _, city in records}
        grouped: dict[str, list[str]] = {}
        for station, _, city in records:
            if city:
                grouped.setdefault(city, []).append(station)
        CITY_STATIONS = {city: tuple(stations) for city, stations in grouped.items()}
    return STATIONS


def resolve_location(name: str) -> dict:
    """Resolve a station, or a 12306 city selector, to its eligible stations."""
    codes = station_codes()
    city_stations = CITY_STATIONS.get(name, ())
    # A city that has multiple entries in the official station list is expanded by
    # 12306 itself when queried through the city's same-named selector.
    if len(city_stations) > 1 and name in codes:
        return {"name": name, "query": name, "stations": frozenset(city_stations), "isCity": True}
    if name in codes:
        return {"name": name, "query": name, "stations": frozenset((name,)), "isCity": False}
    if city_stations:
        # Rare city names without a same-named selector still have a useful
        # station query; the returned rows are restricted to the city's stations.
        return {"name": name, "query": city_stations[0], "stations": frozenset(city_stations), "isCity": True}
    raise ValueError("未在 12306 车站或城市列表中找到，请输入完整站名或地级市名称。")


def official_url(from_name: str, to_name: str, day: str, codes: dict[str, str]) -> str:
    params = {
        "linktypeid": "dc",
        "fs": f"{from_name},{codes[from_name]}",
        "ts": f"{to_name},{codes[to_name]}",
        "date": day,
        "flag": "N,N,Y",
    }
    return "https://kyfw.12306.cn/otn/leftTicket/init?" + urlencode(params, quote_via=quote, safe=",")


def parse_seat(text: str, sale_at: str) -> dict:
    price = re.search(r"票价([\d.]+)元", text)
    availability = re.search(r"余票(候补|有|无|\d+|\*)", text)
    status = availability.group(1) if availability else ("--" if not text or text.strip() == "--" else text.strip())
    if status == "*":
        status = "未开售"
    return {"status": status, "price": price.group(1) if price else "", "saleAt": sale_at if status == "未开售" else ""}


def read_visible_rows(page, from_stations: frozenset[str], to_stations: frozenset[str], day: str,
                      *, high_speed_only: bool) -> list[dict]:
    # City selectors can return several station pairs. Retain only the requested
    # city members, and use G/D/C services for city-expanded high-speed searches.
    raw = page.locator('tr[id^="ticket_"]').evaluate_all("""rows => rows.map(row => {
      const seatText = prefix => {
        const seat = row.querySelector(`[id^="${prefix}"]`);
        return seat?.getAttribute('aria-label') || seat?.getAttribute('title') || seat?.innerText || '';
      };
      const stations = [...row.querySelectorAll('.cdz strong')].map(e => e.textContent.trim());
      const times = [...row.querySelectorAll('.cds strong')].map(e => e.textContent.trim());
      return {
        train: row.querySelector('a.number')?.textContent.trim() || '',
        stations, times, duration: row.querySelector('.ls strong')?.textContent.trim() || '',
        first: seatText('ZY_'), second: seatText('ZE_'),
        notice: row.querySelector('td:last-child')?.textContent.trim() || ''
      };
    })""")
    results = []
    for item in raw:
        if (len(item["stations"]) < 2 or item["stations"][0] not in from_stations
                or item["stations"][1] not in to_stations
                or not re.fullmatch(r"[A-Z]\d{1,5}", item["train"])
                or high_speed_only and not re.fullmatch(r"[GDC]\d{1,5}", item["train"])):
            continue
        if len(item["times"]) < 2 or not re.fullmatch(r"\d{2}:\d{2}", item["duration"]):
            continue
        sale = re.search(r"(\d{1,2})(?:点|:)(\d{2})分?起售", item["notice"])
        sale_at = ""
        if sale:
            sale_day = date.fromisoformat(day) - timedelta(days=14)
            sale_at = f"{sale_day.isoformat()} {int(sale.group(1)):02d}:{int(sale.group(2)):02d}"
        first_seat = parse_seat(item["first"], sale_at)
        second_seat = parse_seat(item["second"], sale_at)
        results.append({
            "train": item["train"], "from": item["stations"][0], "to": item["stations"][1],
            "depart": item["times"][0], "arrive": item["times"][1], "duration": item["duration"],
            "seats": {"first": first_seat, "second": second_seat},
            "status": second_seat["status"], "price": second_seat["price"],
        })
    return results


def search(from_name: str, to_name: str, day: str) -> dict:
    codes = station_codes()
    from_location = resolve_location(from_name)
    to_location = resolve_location(to_name)
    if from_location["stations"] == to_location["stations"]:
        raise ValueError("出发地和到达地不能相同。")
    try:
        requested = date.fromisoformat(day)
    except ValueError as exc:
        raise ValueError("日期格式不正确。") from exc
    if requested < date.today():
        raise ValueError("请选择今天或之后的出发日期。")
    url = official_url(from_location["query"], to_location["query"], day, codes)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        try:
            page = browser.new_page(locale="zh-CN", timezone_id="Asia/Shanghai")
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.locator('tr[id^="ticket_"]').first.wait_for(timeout=30000)
            rows = read_visible_rows(
                page, from_location["stations"], to_location["stations"], day,
                high_speed_only=from_location["isCity"] or to_location["isCity"],
            )
            if not rows:
                raise RuntimeError("官方页面没有返回符合城市范围的 G/D/C 高铁车次，请在 12306 页面核对。")
        finally:
            browser.close()
    return {"rows": rows, "url": url, "from": from_name, "to": to_name, "date": day,
            "fromMode": "city" if from_location["isCity"] else "station",
            "toMode": "city" if to_location["isCity"] else "station",
            "fromStations": sorted({row["from"] for row in rows}),
            "toStations": sorted({row["to"] for row in rows}),
            "queriedAt": datetime.now().astimezone().isoformat(timespec="seconds")}


def rank_leg_choices(choices: list, ideal_time: str) -> list:
    ideal_minutes = int(ideal_time[:2]) * 60 + int(ideal_time[3:])
    durations = [(item[3] - item[2]).total_seconds() / 60 for item in choices]
    shortest, longest = min(durations), max(durations)
    pair_counts: dict[tuple[str, str], int] = {}
    for row, *_ in choices:
        pair = (row["from"], row["to"])
        pair_counts[pair] = pair_counts.get(pair, 0) + 1
    ranked = []
    for item, duration in zip(choices, durations):
        row, _, depart_at, arrive_at = item
        delta = depart_at.hour * 60 + depart_at.minute - ideal_minutes
        time_part = math.exp(-0.5 * (delta / 90) ** 2)
        duration_part = 1 if longest == shortest else (longest - duration) / (longest - shortest)
        score = 100 * ((50 / 85) * time_part + (35 / 85) * duration_part)
        pair_count = pair_counts[(row["from"], row["to"])]
        ranked.append((item, round(score, 1), pair_count))
    ranked.sort(key=lambda entry: (-int(entry[1] / 5), -entry[2], -entry[1], entry[0][3]))
    return ranked


def plan_route(origin: str, cities: list, start: str, stay_days: int, seat: str,
               preferred_times: list) -> dict:
    if seat not in ("first", "second") or not isinstance(cities, list) or not 1 <= len(cities) <= 4:
        raise ValueError("请输入 1 至 4 个目的城市，并选择一等座或二等座。")
    if not isinstance(stay_days, int) or not 1 <= stay_days <= 3:
        raise ValueError("每城停留天数须为 1 至 3 天。")
    if (not isinstance(preferred_times, list) or len(preferred_times) != len(cities) + 1
            or any(not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value)
                   for value in preferred_times)):
        raise ValueError("请为每一段（含返程）填写有效的理想发车时间。")
    names = [origin.strip()] + [str(city).strip() for city in cities]
    if any(not name or len(name) > 30 for name in names) or len(set(names)) != len(names):
        raise ValueError("城市名称不能为空或重复，且每个名称不能超过 30 字。")
    locations = {name: resolve_location(name) for name in names}
    if any(name not in CITY_STATIONS for name in names):
        raise ValueError("路线规划请输入城市名，例如上海、杭州、宁波；精确站名用于上方单程查询。")
    try:
        start_day = date.fromisoformat(start)
    except ValueError as exc:
        raise ValueError("出发日期格式不正确。") from exc
    if not date.today() <= start_day or start_day + timedelta(days=len(cities) * stay_days) > date.today() + timedelta(days=14):
        raise ValueError("所有乘车日期须在今天起 15 天的预售范围内。")

    cache = {}
    query_limit = 24
    limit_reached = False
    checked = 0
    route = None

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        try:
            page = browser.new_page(locale="zh-CN", timezone_id="Asia/Shanghai")

            def available(from_name: str, to_name: str, travel_day: date) -> tuple[list, str] | None:
                nonlocal checked, limit_reached
                key = (from_name, to_name, travel_day)
                if key in cache:
                    return cache[key]
                if checked >= query_limit:
                    limit_reached = True
                    return None
                checked += 1
                departure = locations[from_name]
                arrival = locations[to_name]
                url = official_url(departure["query"], arrival["query"], travel_day.isoformat(), station_codes())
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                try:
                    page.locator('tr[id^="ticket_"]').first.wait_for(timeout=20000)
                except PlaywrightError:
                    if not re.search(r"未找到.*列车|没有符合.*车次|未查询到.*车次", page.locator("body").inner_text()):
                        raise
                rows = read_visible_rows(page, departure["stations"], arrival["stations"], travel_day.isoformat(), high_speed_only=True)
                choices = []
                for row in rows:
                    selected = row["seats"][seat]
                    if not (selected["status"] == "有" or selected["status"].isdigit() and int(selected["status"]) > 0):
                        continue
                    try:
                        fare = float(selected["price"]) if selected["price"] else None
                        depart_at = datetime.fromisoformat(f'{travel_day.isoformat()}T{row["depart"]}')
                        hours, minutes = (int(part) for part in row["duration"].split(":"))
                        arrive_at = depart_at + timedelta(hours=hours, minutes=minutes)
                    except (ValueError, TypeError):
                        continue
                    choices.append((row, fare, depart_at, arrive_at))
                cache[key] = (choices, url)
                return cache[key]

            def connect(current: str, remaining: tuple[str, ...], chosen: list) -> list | None:
                if not remaining and current == names[0]:
                    return chosen
                position = len(chosen)
                travel_day = start_day + timedelta(days=position * stay_days)
                destinations = remaining if remaining else (names[0],)
                for next_city in destinations:
                    result = available(current, next_city, travel_day)
                    if result is None:
                        return None
                    choices, url = result
                    eligible = []
                    for row, fare, depart_at, arrive_at in choices:
                        if chosen:
                            previous = chosen[-1]
                            hours = 4 if row["from"] == previous["train"]["to"] else 6
                            if depart_at - datetime.fromisoformat(previous["arriveAt"]) < timedelta(hours=hours):
                                continue
                        eligible.append((row, fare, depart_at, arrive_at))
                    ranked = rank_leg_choices(eligible, preferred_times[position]) if eligible else []
                    candidate_choices = ranked[:3]
                    if eligible:
                        earliest = min(eligible, key=lambda item: item[3])
                        if all(earliest != item[0] for item in candidate_choices):
                            candidate_choices.append(next(item for item in ranked if item[0] == earliest))
                    for (row, fare, depart_at, arrive_at), score, pair_count in candidate_choices:
                        leg = {"from": current, "to": next_city, "date": travel_day.isoformat(),
                               "train": row, "fare": fare, "departAt": depart_at.isoformat(timespec="minutes"),
                               "arriveAt": arrive_at.isoformat(timespec="minutes"),
                               "score": score, "idealDepart": preferred_times[position],
                               "alternatives": pair_count - 1, "url": url}
                        answer = (chosen + [leg] if not remaining else
                                  connect(next_city, tuple(city for city in remaining if city != next_city), chosen + [leg]))
                        if answer:
                            return answer
                        if limit_reached:
                            return None
                return None

            route = connect(names[0], tuple(names[1:]), [])
        finally:
            browser.close()
    return {"legs": route or [], "complete": route is not None,
            "reason": "查询次数达到上限，尚未穷尽所有顺序。" if limit_reached else "当前日期和席别下未找到能串联全部城市并直达返回出发城市的可购 G/D/C 车次。" if not route else "",
            "checkedPairs": checked,
            "totalFare": round(sum(leg["fare"] for leg in route), 2) if route and all(leg["fare"] is not None for leg in route) else None,
            "queriedAt": datetime.now().astimezone().isoformat(timespec="seconds")}


def finish_order_at_payment(page) -> dict:
    """Submit the already-selected passenger order and stop once payment is shown."""
    submit = page.locator("#submitOrder_id").first
    try:
        submit.wait_for(state="visible", timeout=5_000)
    except PlaywrightError as exc:
        raise RuntimeError("未检测到订单确认页。请先在官方窗口完成登录并选择乘车人。") from exc
    if not submit.is_enabled():
        raise RuntimeError("提交订单按钮不可用，请先在官方窗口选择至少一名乘车人。")
    submit.click()
    confirm = page.locator("#qr_submit_id").first
    try:
        confirm.wait_for(state="visible", timeout=12_000)
    except PlaywrightError as exc:
        raise RuntimeError("订单确认框未出现，12306 可能要求你在官方窗口先处理提示。") from exc
    confirm.click()
    try:
        page.wait_for_url(re.compile(r".*(?:payOrder|payorder|/pay/).*"), timeout=30_000)
    except PlaywrightError:
        try:
            page.get_by_text(re.compile(r"支付")).first.wait_for(state="visible", timeout=5_000)
        except PlaywrightError as exc:
            raise RuntimeError("未能确认已进入付款页，请在官方窗口核对订单状态。") from exc
    return {"state": "payment_ready", "message": "已到达 12306 付款页面，请核对订单后自行完成付款。"}


def select_train(from_name: str, to_name: str, day: str, train: str, seat: str, *, headless: bool, hold: bool,
                 on_ready=None, checkout_event: threading.Event | None = None, on_complete=None) -> dict:
    codes = station_codes()
    if from_name not in codes or to_name not in codes or from_name == to_name:
        raise ValueError("站名无效。")
    try:
        requested = date.fromisoformat(day)
    except ValueError as exc:
        raise ValueError("日期格式不正确。") from exc
    if requested < date.today() or not re.fullmatch(r"[A-Z]\d{1,5}", train) or seat not in ("first", "second"):
        raise ValueError("车次或席别无效。")
    url = official_url(from_name, to_name, day, codes)
    prefix = "ZY_" if seat == "first" else "ZE_"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=headless)
        try:
            page = browser.new_page(locale="zh-CN", timezone_id="Asia/Shanghai", viewport={"width": 1360, "height": 850})
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if not headless:
                page.bring_to_front()
            page.locator('tr[id^="ticket_"]').first.wait_for(timeout=30000)
            row_id = page.locator('tr[id^="ticket_"]').evaluate_all("""(rows, wanted) => {
              const row = rows.find(r => r.querySelector('a.number')?.textContent.trim() === wanted.train &&
                [...r.querySelectorAll('.cdz strong')].map(x => x.textContent.trim()).join('|') === `${wanted.from}|${wanted.to}`);
              return row?.id || '';
            }""", {"train": train, "from": from_name, "to": to_name})
            if not row_id:
                raise RuntimeError("官方页面中没有找到该车次和车站组合。")
            row = page.locator(f"#{row_id}")
            seat_cell = row.locator(f'[id^="{prefix}"]')
            if not seat_cell.count():
                raise RuntimeError("该车次未提供所选席别。")
            seat_text = seat_cell.get_attribute("aria-label") or seat_cell.inner_text()
            status = parse_seat(seat_text, "")["status"]
            row.scroll_into_view_if_needed()
            row.evaluate("element => { element.style.outline = '3px solid #087f69'; element.style.outlineOffset = '-3px'; }")
            if status == "未开售":
                result = {"state": "not_on_sale", "message": "该席别尚未开售，已在官方页面定位车次。"}
            elif status == "候补":
                seat_cell.click()
                result = {"state": "standby", "message": "已在官方页面选择候补席别，请在打开的窗口继续。"}
            elif status == "有" or status.isdigit() and int(status) > 0:
                book = row.locator("a.btn72")
                if not book.count():
                    raise RuntimeError("官方页面此刻未提供预订入口，请在打开的窗口核对余票。")
                book.click()
                result = {"state": "awaiting_checkout", "message": "已在 12306 点击预订。请在官方窗口完成登录并选择乘车人，再回到本网页继续到付款页。"}
            else:
                result = {"state": "unavailable", "message": "该席别目前不可选，已在官方页面定位车次。"}
            result["url"] = url
            if on_ready:
                on_ready(result)
            if result["state"] == "awaiting_checkout" and checkout_event:
                if checkout_event.wait(timeout=3_600_000):
                    try:
                        result = finish_order_at_payment(page)
                    except (PlaywrightError, RuntimeError) as exc:
                        result = {"state": "checkout_error", "message": str(exc)}
                    result["url"] = page.url
                    if on_complete:
                        on_complete(result)
            if hold:
                try:
                    page.wait_for_event("close", timeout=3_600_000)
                except PlaywrightError:
                    pass
            return result
        finally:
            browser.close()


def select_in_window(from_name: str, to_name: str, day: str, train: str, seat: str) -> dict:
    ready = threading.Event()
    checkout_event = threading.Event()
    checkout_complete = threading.Event()
    output: dict = {}
    checkout_token = uuid.uuid4().hex
    with CHECKOUT_LOCK:
        ACTIVE_CHECKOUTS[checkout_token] = {"event": checkout_event, "complete": checkout_complete, "output": output}

    def run() -> None:
        try:
            # Keep the browser alive after responding so login stays in the official window.
            select_train(from_name, to_name, day, train, seat, headless=False, hold=True,
                         checkout_event=checkout_event,
                         on_ready=lambda result: (output.update(result), ready.set()),
                         on_complete=lambda result: (output.update(result), checkout_complete.set()))
        except (PlaywrightError, OSError, RuntimeError, ValueError) as exc:
            output["error"] = f"无法在官方页面选择车次：{exc}"
            ready.set()
            checkout_complete.set()
        finally:
            with CHECKOUT_LOCK:
                ACTIVE_CHECKOUTS.pop(checkout_token, None)

    threading.Thread(target=run, daemon=True, name="12306-selection").start()
    if not ready.wait(timeout=75):
        raise RuntimeError("打开官方页面超时。")
    if "error" in output:
        raise RuntimeError(output["error"])
    if output.get("state") == "awaiting_checkout":
        output["checkoutToken"] = checkout_token
    return output


def advance_checkout(checkout_token: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", checkout_token):
        raise ValueError("付款流程标识无效。")
    with CHECKOUT_LOCK:
        checkout = ACTIVE_CHECKOUTS.get(checkout_token)
    if not checkout:
        raise RuntimeError("该预订窗口已关闭或已过期，请重新选择车次。")
    checkout["event"].set()
    if not checkout["complete"].wait(timeout=55):
        raise RuntimeError("官方页面仍在处理中，请在打开的窗口稍候。")
    if checkout["output"].get("state") != "payment_ready":
        raise RuntimeError(checkout["output"].get("message", "未能进入付款页。"))
    return checkout["output"]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        # Windowed packaged builds have no stderr; request logs are not needed.
        return

    def send_json(self, status: int, body: dict) -> None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        if self.path == "/api/app-info":
            self.send_json(200, {"lanUrl": f"http://{local_lan_ip()}:8765/"})
            return
        assets = {
            "/": PAGE,
            "/12306-live.html": PAGE,
            "/manifest.webmanifest": ROOT / "outputs" / "manifest.webmanifest",
            "/service-worker.js": ROOT / "outputs" / "service-worker.js",
            "/app-icon.svg": ROOT / "outputs" / "app-icon.svg",
        }
        asset = assets.get(self.path)
        if not asset or not asset.is_file():
            self.send_error(404)
            return
        payload = asset.read_bytes()
        self.send_response(200)
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".webmanifest": "application/manifest+json; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
        }.get(asset.suffix, "application/octet-stream")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        if self.path not in ("/api/search", "/api/select", "/api/advance", "/api/plan", "/api/path"):
            self.send_error(404)
            return
        try:
            origin = self.headers.get("Origin")
            # if origin and origin != "http://127.0.0.1:8765":
            #     raise ValueError("仅接受本地页面的查询。")
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                raise ValueError("请使用 JSON 查询。")
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 2048:
                raise ValueError("查询参数无效。")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("查询参数无效。")
            if self.path == "/api/path":
                names = data.get("stations")
                if not isinstance(names, list) or len(names) > 6 or any(not isinstance(x, str) or len(x) > 30 for x in names):
                    raise ValueError("请按顺序输入 2 至 6 个城市。")
                try:
                    start = datetime.fromisoformat(str(data.get("start", "")))
                except ValueError as exc:
                    raise ValueError("出发日期或时间无效。") from exc
                if start.date() < date.today() or start.date() > date.today() + timedelta(days=14):
                    raise ValueError("请选择今天起 15 天内的出发日期。")
                timetable, source_info = rail_timetable()
                result = timetable.find([x.strip() for x in names], start, str(data.get("mode", "all")),
                                        str(data.get("preferredTime", start.strftime("%H:%M"))))
                result["data"] = source_info
                self.send_json(200, result)
                return
            if self.path == "/api/advance":
                self.send_json(200, advance_checkout(str(data.get("checkoutToken", "")).strip()) )
                return
            if self.path == "/api/plan":
                global LAST_QUERY
                if not LOCK.acquire(blocking=False):
                    self.send_json(429, {"error": "另一项车次查询仍在进行，请稍后再试。"})
                    return
                try:
                    if time.monotonic() - LAST_QUERY < 15:
                        self.send_json(429, {"error": "请至少间隔 15 秒再查询。"})
                        return
                    LAST_QUERY = time.monotonic()
                    self.send_json(200, plan_route(
                        str(data.get("origin", "")), data.get("cities"),
                        str(data.get("date", "")), data.get("stayDays"), str(data.get("seat", "")),
                        data.get("preferredTimes"),
                    ))
                finally:
                    LOCK.release()
                return
            from_name = str(data.get("from", "")).strip()
            to_name = str(data.get("to", "")).strip()
            day = str(data.get("date", "")).strip()
            if max(len(from_name), len(to_name), len(day)) > 30:
                raise ValueError("查询参数过长。")
            if self.path == "/api/select":
                train = str(data.get("train", "")).strip().upper()
                seat = str(data.get("seat", "")).strip()
                if not re.fullmatch(r"[A-Z]\d{1,5}", train) or seat not in ("first", "second"):
                    raise ValueError("车次或席别无效。")
                if from_name not in station_codes() or to_name not in station_codes() or from_name == to_name:
                    raise ValueError("站名无效。")
                if date.fromisoformat(day) < date.today():
                    raise ValueError("请选择今天或之后的出发日期。")
                global LAST_SELECTION
                if not SELECTION_LOCK.acquire(blocking=False):
                    self.send_json(429, {"error": "上一项选择尚未完成，请稍后再试。"})
                    return
                try:
                    if time.monotonic() - LAST_SELECTION < 10:
                        self.send_json(429, {"error": "请稍后再选择车次。"})
                        return
                    LAST_SELECTION = time.monotonic()
                    self.send_json(200, select_in_window(from_name, to_name, day, train, seat))
                finally:
                    SELECTION_LOCK.release()
                return
            if not LOCK.acquire(blocking=False):
                self.send_json(429, {"error": "上一项查询尚未结束，请稍后再试。"})
                return
            try:
                if time.monotonic() - LAST_QUERY < 15:
                    self.send_json(429, {"error": "请至少间隔 15 秒再查询。"})
                    return
                LAST_QUERY = time.monotonic()
                result = search(from_name, to_name, day)
            finally:
                LOCK.release()
            self.send_json(200, result)
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})
        except (PlaywrightError, OSError, RuntimeError) as exc:
            self.send_json(502, {"error": f"无法读取外部查询结果：{exc}"})


def local_lan_ip() -> str:
    """Return the active LAN address without sending a network request."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("8.8.8.8", 80))
            return probe.getsockname()[0]
        except OSError:
            return "127.0.0.1"


if __name__ == "__main__":
    open_browser = "--open-browser" in __import__("sys").argv
    try:
        server = ThreadingHTTPServer(("0.0.0.0", 8765), Handler)
    except OSError:
        if open_browser:
            webbrowser.open("http://127.0.0.1:8765/")
            raise SystemExit(0)
        raise
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open("http://127.0.0.1:8765/")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
