import os
import re
import json
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
LINE_USER_ID = os.environ.get("LINE_USER_ID")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

CACHE_FILE = "notified_urls.txt"
ORGANIZERS_FILE = "organizers.json"
# 「PiRiRi 非出演」と判定したイベントの終了日を記録するファイル。
# 開催済み（終了日が過去）のイベントだけを次回以降スキップする。開催前のイベントは毎回再検証する
# （後からPiRiRiの出演が追加される場合があるため）。
CHECKED_FILE = "past_events.json"
PAST_MARGIN_DAYS = 1                # 終了日からこの日数を過ぎたら「開催済み」とみなす
JST = ZoneInfo("Asia/Tokyo")
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "4"))  # 詳細ページの並列数
DEBUG_DIR = "debug_pages"  # 抽出に失敗したページのテキスト/HTMLを保存する場所

UNKNOWN = "要確認（詳細ページ参照）"


def load_organizers():
    if os.path.exists(ORGANIZERS_FILE):
        try:
            with open(ORGANIZERS_FILE, "r", encoding="utf-8") as f:
                organizers = json.load(f)
                print(f"[DEBUG] {len(organizers)} 件の主催者を設定ファイルから読み込みました。")
                return organizers
        except Exception as e:
            print(f"[WARN] 設定ファイルの読み込みエラー ({ORGANIZERS_FILE}): {e}")
    else:
        print(f"[WARN] {ORGANIZERS_FILE} が見つかりません。デフォルトの検索のみ実行します。")
    return []


def load_notified_urls():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            urls = set(line.strip() for line in f if line.strip())
            print(f"[DEBUG] キャッシュから読み込んだ通知済みURL数: {len(urls)}")
            return urls
    print("[DEBUG] キャッシュファイルが存在しません（初回実行）。")
    return set()


def save_notified_urls(new_urls, existing_urls):
    all_urls = existing_urls.union(new_urls)
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        for url in sorted(all_urls):
            f.write(f"{url}\n")
    print(f"[DEBUG] キャッシュに合計 {len(all_urls)} 件保存しました。")


def is_PiRiRi_performing(soup):
    """
    イベント詳細ページのメイン本文エリア内に PiRiRi が出演者として記載されているか高精度に判定
    """
    soup_copy = BeautifulSoup(str(soup), "html.parser")
    for unwanted in soup_copy.select(
        ".recommend, .other-events, .related-events, footer, header, #header, "
        ".sidebar, .other-event-list, .recommend-event, .seller-event, "
        "[class*='recommend'], [class*='other'], [id*='recommend'], [id*='other']"
    ):
        unwanted.decompose()

    pattern = re.compile(r'piriri(?:\.|\b)', re.IGNORECASE)

    title_element = soup_copy.find("h1") or soup_copy.find("title")
    if title_element and pattern.search(title_element.get_text()):
        return True

    main_content = soup_copy.select_one("#event-detail, .event-detail, .main-content, #main")
    target_soup = main_content if main_content else soup_copy

    blocks = target_soup.find_all(["div", "p", "li", "td", "span", "dd", "dt"])

    for block in blocks:
        block_text = block.get_text(strip=True)
        if pattern.search(block_text):
            if len(block_text) < 300:
                print(f"[CHECK] 正確な「PiRiRi」の一致を確認: {block_text[:50]}")
                return True

    return False


# =====================================================================
# 開催日時・販売期間の抽出
# =====================================================================

DATE_STRICT = r'20\d{2}\s*(?:[/.\-]|年)\s*\d{1,2}\s*(?:[/.\-]|月)\s*\d{1,2}\s*日?'
DATE_LOOSE = r'(?:20\d{2}\s*(?:[/.\-]|年)\s*)?\d{1,2}\s*(?:[/.\-]|月)\s*\d{1,2}\s*日?'
WEEKDAY = r'(?:\s*[\(（][^\)）]{1,4}[\)）])?'
TIME = r'\d{1,2}\s*[:：]\s*\d{2}'
DT_STRICT = rf'{DATE_STRICT}{WEEKDAY}(?:\s*{TIME})?'
DT_LOOSE = rf'{DATE_LOOSE}{WEEKDAY}(?:\s*{TIME})?'
RANGE_SEP = r'(?:[～〜~–—]|\s-\s)'
RANGE_ANY = re.compile(rf'{DT_STRICT}\s*{RANGE_SEP}\s*(?:{DT_LOOSE}|{TIME})?')
RANGE_FULL = re.compile(rf'{DT_STRICT}\s*{RANGE_SEP}\s*{DT_LOOSE}')

DATE_LABELS = ["開催日時", "公演日時", "開催日", "公演日", "日時", "日程", "開催期間", "date"]
SALES_LABELS = [
    "チケット販売期間", "販売期間", "受付期間", "申込期間", "申し込み期間",
    "発売期間", "販売日程", "受付日程", "一般発売", "販売開始", "発売日",
    "sales period", "reception period", "application period",
]
SALES_KW = re.compile(r'販売|発売|受付|申込|申し込み|締切|締め切り|まで|から')
NOT_EVENT_DATE_KW = re.compile(r'販売|発売|受付|申込|申し込み|締切|締め切り|期限|更新|投稿|公開|まで|から|入金|支払')

CONT_RE = re.compile(
    r'^(?:[\(（]|\d{1,2}\s*[:：]\s*\d{2}|20\d{2}\s*[/.\-年]|開場|開演|OPEN|START|Open|Start|open|start|[～〜~–—])'
)

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_WD = {"mon": "月", "tue": "火", "wed": "水", "thu": "木", "fri": "金", "sat": "土", "sun": "日"}


def _normalize_en(line):
    def repl(m):
        mon = _MONTHS.get(m.group(2)[:3].lower())
        if not mon:
            return m.group(0)
        return f"{m.group(1)}/{mon:02d}/{int(m.group(3)):02d}"

    line = re.sub(
        r'(20\d{2})\s*(?:year(?:\(s\)|s)?)?\s*([A-Za-z]{3,9})\.?\s*(\d{1,2})\s*(?:day(?:\(s\)|s)?)?',
        repl, line)
    line = re.sub(r'\((Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*\)',
                  lambda m: f"({_WD[m.group(1).lower()]})", line)
    return line


def _clean_lines(text):
    lines = []
    for raw in (text or "").splitlines():
        line = re.sub(r'[ \t\u3000\xa0]+', ' ', raw).strip()
        if line:
            lines.append(_normalize_en(line))
    return lines


def _soup_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "header", "footer"]):
        t.decompose()
    for t in soup.select(".recommend, .other-events, .related-events, .recommend-event, .other-event-list"):
        t.decompose()
    return soup.get_text("\n", strip=True)


def _labeled_values(lines, labels, strict=False):
    date_pat = DT_STRICT if strict else DT_LOOSE
    for i, line in enumerate(lines):
        for label in labels:
            if not line.lower().startswith(label.lower()):
                continue
            rest = line[len(label):].lstrip(" :：")
            cands = [(rest, i)] if rest else []
            cands += [(lines[j], j) for j in range(i + 1, min(i + 4, len(lines)))]
            matched = False
            for text, j in cands:
                if len(text) < 150 and re.search(date_pat, text):
                    parts = [text]
                    for k in range(j + 1, min(j + 4, len(lines))):
                        if len(lines[k]) < 40 and CONT_RE.match(lines[k]):
                            parts.append(lines[k])
                        else:
                            break
                    yield re.sub(r'\s+', ' ', " ".join(parts)).strip()
                    matched = True
                    break
            if matched:
                break


def _labeled_value(lines, labels, strict=False):
    return next(_labeled_values(lines, labels, strict), "")


def _guess_event_date(lines):
    for line in lines:
        if (len(line) < 150 and re.search(DT_STRICT, line)
                and re.search(r'開場|開演|OPEN|START', line, re.IGNORECASE)
                and not NOT_EVENT_DATE_KW.search(line)):
            return re.sub(r'\s+', ' ', line).strip()
    for line in lines:
        if len(line) < 80 and re.search(DT_STRICT, line) and not NOT_EVENT_DATE_KW.search(line):
            return re.sub(r'\s+', ' ', line).strip()
    return ""


SECTION_CUT_RE = re.compile(r'同じ(?:主催|販売元)|販売元の他|主催者の他|Events from the same', re.IGNORECASE)


def _extract_sales(lines, event_date=""):
    found = []

    def add(s):
        s = re.sub(r'\s+', ' ', s).strip()
        if not s or len(s) >= 150:
            return
        if event_date and s == re.sub(r'\s+', ' ', event_date).strip():
            return
        if any(s in f or f in s for f in found):
            return
        found.append(s)

    for v in _labeled_values(lines, SALES_LABELS):
        add(v)
    if found:
        return " / ".join(found[:4])

    for idx, line in enumerate(lines):
        if SECTION_CUT_RE.search(line):
            lines = lines[:idx]
            break

    for line in lines:
        if any(line.lower().startswith(l.lower()) for l in DATE_LABELS):
            continue
        if len(line) < 150 and RANGE_ANY.search(line) and _collapse_same_day(line) == line:
            add(line)

    flat = " ".join(lines)
    for m in RANGE_FULL.finditer(flat):
        if _collapse_same_day(m.group(0)) == m.group(0):
            add(m.group(0))

    if not found:
        for line in lines:
            if any(line.lower().startswith(l.lower()) for l in DATE_LABELS):
                continue
            if len(line) < 100 and SALES_KW.search(line) and re.search(DT_LOOSE, line):
                add(line)

    return " / ".join(found[:4])


def _collapse_same_day(s):
    parts = re.split(r'\s*[～〜~–—]\s*', s)
    if len(parts) == 2:
        d1, d2 = re.search(DATE_STRICT, parts[0]), re.search(DATE_STRICT, parts[1])
        if d1 and d2 and re.sub(r'\s+', '', d1.group(0)) == re.sub(r'\s+', '', d2.group(0)):
            return parts[0].strip()
    return s


def _find_open_start(lines):
    pat = re.compile(
        r'(?:OPEN|開場)\s*\d{1,2}[:：]\d{2}(?:\s*[/／]\s*(?:START|開演)\s*\d{1,2}[:：]\d{2})?',
        re.IGNORECASE)
    for line in lines:
        if len(line) < 80:
            m = pat.search(line)
            if m:
                return m.group(0)
    return ""


def _iter_dicts(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_dicts(v)


def _fmt_iso(s):
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?', str(s or ""))
    if not m:
        return ""
    y, mo, d, hh, mm = m.groups()
    return f"{y}/{mo}/{d}" + (f" {hh}:{mm}" if hh and (hh, mm) != ("00", "00") else "")


def _from_jsonld(html):
    result = {"date": "", "sales": ""}
    soup = BeautifulSoup(html, "html.parser")
    for s in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(s.string or s.get_text())
        except Exception:
            continue
        for node in _iter_dicts(data):
            t = node.get("@type")
            types = t if isinstance(t, list) else [t]
            if not any(isinstance(x, str) and x.endswith("Event") for x in types):
                continue
            start = _fmt_iso(node.get("startDate"))
            end = _fmt_iso(node.get("endDate"))
            if start and not result["date"]:
                result["date"] = start if (not end or end == start) else f"{start} ～ {end}"
            offers = node.get("offers")
            offers = offers if isinstance(offers, list) else ([offers] if offers else [])
            periods = []
            for o in offers:
                if not isinstance(o, dict):
                    continue
                vf, vt = _fmt_iso(o.get("validFrom")), _fmt_iso(o.get("validThrough"))
                if vf or vt:
                    p = f"{vf} ～ {vt}".strip()
                    if p not in periods:
                        periods.append(p)
            if periods and not result["sales"]:
                result["sales"] = " / ".join(periods[:4])
    return result


def _dump_debug(url, body_text, html):
    lines = _clean_lines(body_text)
    html = html or ""
    print(f"----- [DUMP] {url} -----")
    print(f"  body行数={len(lines)} / html長={len(html)} / iframe数={html.count('<iframe')}")
    for l in lines[:150]:
        print(f"  | {l[:120]}")
    print("----- [DUMP END] -----")

    try:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        slug = re.sub(r'[^A-Za-z0-9_-]+', "_", url.rstrip("/").split("/")[-1])[:60] or "page"
        with open(os.path.join(DEBUG_DIR, f"{slug}.txt"), "w", encoding="utf-8") as f:
            f.write(body_text or "")
        with open(os.path.join(DEBUG_DIR, f"{slug}.html"), "w", encoding="utf-8") as f:
            f.write(html)
    except Exception as e:
        print(f"[WARN] デバッグ保存失敗: {e}")


def extract_event_details_from_page(page, url="", scroll=False):
    if scroll:
        try:
            for ratio in (1 / 3, 2 / 3, 1):
                page.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {ratio});")
                page.wait_for_timeout(300)
            page.wait_for_timeout(500)
        except Exception:
            pass

    try:
        body_text = page.inner_text("body")
    except Exception:
        body_text = ""
    html = page.content()

    texts = [_clean_lines(body_text), _clean_lines(_soup_text(html))]
    ld = _from_jsonld(html)

    # --- 開催日時 ---
    event_date = ""
    event_lines = texts[0]
    for lines in texts:
        event_date = _labeled_value(lines, DATE_LABELS)
        if event_date:
            event_lines = lines
            break
    if not event_date:
        event_date = ld["date"]
    if not event_date:
        for lines in texts:
            event_date = _guess_event_date(lines)
            if event_date:
                event_lines = lines
                break

    event_date_raw = event_date
    if event_date:
        event_date = _collapse_same_day(event_date)
        if not re.search(TIME, event_date):
            open_start = _find_open_start(event_lines) or _find_open_start(texts[1])
            if open_start:
                event_date = f"{event_date} {open_start}"

    # --- 販売期間 ---
    sales_period = ""
    for lines in texts:
        sales_period = _extract_sales(lines, event_date_raw)
        if sales_period:
            break
    if not sales_period:
        sales_period = ld["sales"]

    if (not event_date or not sales_period) and not scroll:
        return extract_event_details_from_page(page, url, scroll=True)

    if not event_date or not sales_period:
        print(f"[DEBUG] 抽出不足 date={bool(event_date)} sales={bool(sales_period)} → ダンプ保存")
        _dump_debug(url, body_text, html)

    return (event_date or UNKNOWN), (sales_period or UNKNOWN)


BLOCK_RESOURCE_TYPES = {"image", "media", "font"}
BLOCK_URL_KEYWORDS = (
    "googletagmanager.com", "google-analytics.com", "doubleclick.net", "googlesyndication.com",
    "facebook.net", "facebook.com/tr", "wovn.io", "clarity.ms", "hotjar.com",
    "ads-twitter.com", "analytics.twitter.com", "adsrvr.org", "criteo",
)


def _route_handler(route):
    req = route.request
    if req.resource_type in BLOCK_RESOURCE_TYPES or any(k in req.url for k in BLOCK_URL_KEYWORDS):
        route.abort()
    else:
        route.continue_()


def _new_context(p):
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        viewport={'width': 1280, 'height': 800},
        locale="ja-JP",
        timezone_id="Asia/Tokyo",
        extra_http_headers={"Accept-Language": "ja-JP,ja;q=0.9"},
    )
    context.route("**/*", _route_handler)
    return browser, context


def _event_end_date(lines):
    raw = _labeled_value(lines, DATE_LABELS)
    if not raw:
        return None
    found = re.findall(r'(20\d{2})\s*(?:[/.\-]|年)\s*(\d{1,2})\s*(?:[/.\-]|月)\s*(\d{1,2})', raw)
    dates = []
    for y, m, d in found:
        try:
            dates.append(date(int(y), int(m), int(d)))
        except ValueError:
            pass
    return max(dates) if dates else None


def load_checked_urls():
    if os.path.exists(CHECKED_FILE):
        try:
            with open(CHECKED_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception as e:
            print(f"[WARN] {CHECKED_FILE} の読み込みエラー: {e}")
    return {}


def is_past_event(url, checked):
    d = checked.get(url)
    if not d:
        return False
    try:
        end = date.fromisoformat(d)
    except Exception:
        return False
    return end < datetime.now(JST).date() - timedelta(days=PAST_MARGIN_DAYS)


def save_checked_urls(checked):
    limit = datetime.now(JST).date() - timedelta(days=365)
    pruned = {}
    for url, d in checked.items():
        try:
            if date.fromisoformat(d) >= limit:
                pruned[url] = d
        except Exception:
            pass
    with open(CHECKED_FILE, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(pruned.items())), f, ensure_ascii=False, indent=0)
    print(f"[DEBUG] 非出演イベントの記録を {len(pruned)} 件保存しました。")


def _check_urls_worker(urls):
    """1スレッド = 1ブラウザで、担当URLを順に検証する。戻り値: (出演イベント, 非出演と確定したURL)"""
    events, excluded = [], []
    if not urls:
        return events, excluded

    with sync_playwright() as p:
        browser, context = _new_context(p)
        page = context.new_page()

        for url in urls:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                try:
                    page.wait_for_selector("h1", timeout=8000)
                except Exception:
                    pass

                detail_html = page.content()
                detail_soup = BeautifulSoup(detail_html, "html.parser")

                if is_PiRiRi_performing(detail_soup):
                    title_tag = detail_soup.find("h1") or detail_soup.find("title")
                    raw_title = title_tag.get_text(strip=True) if title_tag else "PiRiRi 出演ライブ"

                    clean_title = raw_title.replace(" - LivePocket-Ticket-", "").replace("｜LivePocket", "")
                    clean_title = re.sub(r'\s+', ' ', clean_title).strip()
                    if len(clean_title) > 70:
                        clean_title = clean_title[:70] + "..."

                    event_date, sales_period = extract_event_details_from_page(page, url)

                    print(f"[MATCH] ★PiRiRiの出演を確認！: {clean_title} ({url})")
                    print(f"        📅 日程: {event_date}")
                    print(f"        🎟 販売期間: {sales_period}")

                    events.append({
                        "title": clean_title,
                        "url": url,
                        "date": event_date,
                        "sales_period": sales_period,
                    })
                else:
                    print(f"[EXCLUDE] PiRiRi非出演のため除外: {url}")
                    end_date = _event_end_date(_clean_lines(_soup_text(detail_html)))
                    if detail_soup.find("h1") and end_date:
                        excluded.append((url, end_date.isoformat()))

            except Exception as e:
                print(f"[WARN] 詳細検証失敗 ({url}): {e}")

        browser.close()

    return events, excluded


def fetch_piriri_events(notified_urls):
    t_start = time.time()
    candidate_urls = set()
    checked = load_checked_urls()
    skipped_checked = 0

    organizers = load_organizers()

    search_urls = [
        "https://livepocket.jp/event/search?search_word=PiRiRi",
    ] + [org["url"] for org in organizers if "url" in org]

    # 1. 各ソースからイベント詳細URLを収集
    with sync_playwright() as p:
        browser, context = _new_context(p)
        page = context.new_page()

        for target_url in search_urls:
            print(f"[DEBUG] ページをスキャン中: {target_url}")
            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
                try:
                    page.wait_for_selector('a[href*="/e/"], a[href*="/event/detail/"]', timeout=10000)
                except Exception:
                    pass

                soup = BeautifulSoup(page.content(), "html.parser")

                for a_tag in soup.find_all("a", href=True):
                    href = a_tag["href"]
                    if "/e/" not in href and "/event/detail/" not in href:
                        continue

                    full_url = href if href.startswith("http") else f"https://livepocket.jp{href}"
                    clean_url = full_url.split("?")[0]

                    if "/event/search" in clean_url:
                        continue
                    if clean_url in notified_urls:
                        continue
                    if is_past_event(clean_url, checked):
                        skipped_checked += 1
                        continue

                    candidate_urls.add(clean_url)
            except Exception as e:
                print(f"[WARN] スキャン失敗 ({target_url}): {e}")

        browser.close()

    candidates = sorted(candidate_urls)
    print(f"[DEBUG] 収集された検証対象のイベント総数: {len(candidates)}"
          f"（開催済みで省略: {skipped_checked} 件）  [{time.time() - t_start:.1f}s]")

    # 2. 詳細ページを並列に検証・情報抽出
    new_events = []
    excluded_all = []
    if candidates:
        n = max(1, min(MAX_WORKERS, len(candidates)))
        chunks = [candidates[i::n] for i in range(n)]
        print(f"[DEBUG] {n} 並列で詳細ページを検証します。")
        with ThreadPoolExecutor(max_workers=n) as ex:
            for events, excluded in ex.map(_check_urls_worker, chunks):
                new_events.extend(events)
                excluded_all.extend(excluded)

    for u, end_iso in excluded_all:
        checked[u] = end_iso
    save_checked_urls(checked)

    new_events.sort(key=lambda e: e["url"])
    print(f"[DEBUG] 最終抽出された「PiRiRi」出演ライブ数: {len(new_events)}"
          f"  [合計 {time.time() - t_start:.1f}s]")
    return new_events


LINE_TEXT_LIMIT = 4500
DISCORD_TEXT_LIMIT = 1900


def build_message_chunks(events, limit):
    header = "🎉 【PiRiRi】出演のチケット・ライブ情報が見つかりました！\n\n"
    blocks = [
        f"📌 {e['title']}\n"
        f"📅 日程: {e['date']}\n"
        f"🎟 販売期間: {e['sales_period']}\n"
        f"🔗 {e['url']}"
        for e in events
    ]

    chunks = []
    current = header
    for block in blocks:
        candidate = current + block + "\n\n"
        if len(candidate) > limit and current.strip() and current != header:
            chunks.append(current.strip())
            current = block + "\n\n"
        else:
            current = candidate
    if current.strip():
        chunks.append(current.strip())
    return chunks


def send_line_notification(events):
    if not events:
        print("[INFO] 送信する新着イベントがありません。")
        return False

    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_USER_ID:
        print("[INFO] LINEの認証情報が未設定のため、LINE通知をスキップします。")
        return False

    endpoint = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"
    }

    all_ok = True
    for chunk in build_message_chunks(events, LINE_TEXT_LIMIT):
        payload = {
            "to": LINE_USER_ID,
            "messages": [{"type": "text", "text": chunk}]
        }
        res = requests.post(endpoint, json=payload, headers=headers, timeout=30)
        print(f"[DEBUG] LINE APIレスポンスコード: {res.status_code}")
        print(f"[DEBUG] LINE APIレスポンス詳細: {res.text}")
        if res.status_code != 200:
            print(f"[ERROR] LINE送信失敗: {res.status_code}")
            all_ok = False

    if all_ok:
        print("[SUCCESS] LINEへの通知が成功しました！")
    return all_ok


def send_discord_notification(events):
    if not events:
        return False

    if not DISCORD_WEBHOOK_URL:
        print("[INFO] DISCORD_WEBHOOK_URL が未設定のため、Discord通知をスキップします。")
        return False

    all_ok = True
    for chunk in build_message_chunks(events, DISCORD_TEXT_LIMIT):
        payload = {
            "content": chunk,
            "allowed_mentions": {"parse": []},
        }
        res = None
        for attempt in range(2):
            res = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=30)
            if res.status_code == 429 and attempt == 0:
                try:
                    wait = float(res.json().get("retry_after", 1))
                except Exception:
                    wait = 1.0
                print(f"[WARN] Discordのレート制限。{wait}秒待って再送します。")
                time.sleep(min(wait, 10))
                continue
            break

        print(f"[DEBUG] Discord APIレスポンスコード: {res.status_code}")
        if res.status_code not in (200, 204):
            print(f"[ERROR] Discord送信失敗: {res.status_code} {res.text[:200]}")
            all_ok = False

    if all_ok:
        print("[SUCCESS] Discordへの通知が成功しました！")
    return all_ok


if __name__ == "__main__":
    notified_urls = load_notified_urls()
    new_events = fetch_piriri_events(notified_urls)

    if new_events:
        results = {
            "LINE": send_line_notification(new_events),
            "Discord": send_discord_notification(new_events),
        }
        for name, ok in results.items():
            print(f"[RESULT] {name}: {'成功' if ok else '失敗/スキップ'}")

        if any(results.values()):
            new_urls = {e["url"] for e in new_events}
            save_notified_urls(new_urls, notified_urls)
        else:
            print("[ERROR] どの通知先にも送信できなかったため、キャッシュは更新しません。")
