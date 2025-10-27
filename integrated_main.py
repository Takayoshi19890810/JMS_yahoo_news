# -*- coding: utf-8 -*-
"""
Yahooニュース 統合スクレイパー（JMS/Mobility Show 専用・GitHub Actions対応）
-------------------------------------------------------------------------------
■ 機能
1) Yahoo!ニュース検索から記事リストを取得（キーワード: JMS, モビリティショー, mobility show）
   取得項目: ソース/タイトル/URL/投稿日/掲載元（= 出力シート A〜E）
2) SOURCEシート（"Yahoo"）に追記（URL重複はスキップ）
3) SOURCEシートのうち「前日15:00〜当日14:59:59」の記事を当日タブ(YYMMDD)へ転送（A〜E）
4) 当日タブの記事ごとに本文・コメントを取得
   - 本文: F..O（最大10ページ）
   - コメント数: P
   - コメント本文: Q..（1ページ=最大10件をJSON文字列で1セルに格納）

■ 認証
- 環境変数 GOOGLE_CREDENTIALS または GCP_SERVICE_ACCOUNT_KEY を優先。
- それらが無ければ、リポジトリ直下の credentials.json を参照。

■ 必要ライブラリ（requirements.txt）
gspread
oauth2client
selenium
webdriver_manager
beautifulsoup4
requests
-------------------------------------------------------------------------------
"""

import os
import json
import time
import re
import random
from datetime import datetime, timedelta, timezone
from typing import List, Tuple, Optional, Set

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from bs4 import BeautifulSoup
import requests

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager

# ========================= 設定 =========================
# ✅ あなたのスプレッドシートID（例: https://docs.google.com/spreadsheets/d/<ここ>/edit）
SHARED_SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "1-oJl5lnTC2FRqayHsPW1ZVqfktAE99PCK6HGmKHiR28")

# Yahoo!ニュースの検索キーワード
KEYWORDS = ["JMS", "モビリティショー", "mobility show"]

# ニュースリストの保存先
SOURCE_SPREADSHEET_ID = SHARED_SPREADSHEET_ID
SOURCE_SHEET_NAME = "Yahoo"

# 本文・コメントの保存先
DEST_SPREADSHEET_ID = SHARED_SPREADSHEET_ID

# 本文・コメント 取得設定
MAX_BODY_PAGES = 10
REQ_HEADERS = {"User-Agent": "Mozilla/5.0"}
MAX_TOTAL_COMMENTS = 5000  # 事故防止の上限

# タイムゾーン
TZ_JST = timezone(timedelta(hours=9))

# ===================== ユーティリティ ====================
def jst_now() -> datetime:
    return datetime.now(TZ_JST)

def format_datetime(dt_obj) -> str:
    return dt_obj.strftime("%Y/%m/%d %H:%M")

def format_yy_m_d_hm(dt: datetime) -> str:
    yy = dt.strftime("%y")
    m = str(int(dt.strftime("%m")))
    d = str(int(dt.strftime("%d")))
    hm = dt.strftime("%H:%M")
    return f"{yy}/{m}/{d} {hm}"

def parse_post_date(raw, today_jst: datetime) -> Optional[datetime]:
    """
    SOURCEシートC列（投稿日）を JST datetime に変換
    許容: "MM/DD HH:MM"（当年補完）, "YYYY/MM/DD HH:MM", "YYYY/MM/DD HH:MM:SS", Excelシリアル
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        s = raw.strip()
        for fmt in ("%m/%d %H:%M", "%Y/%m/%d %H:%M", "%Y/%m/%d %H:%M:%S"):
            try:
                dt = datetime.strptime(s, fmt)
                if fmt == "%m/%d %H:%M":
                    dt = dt.replace(year=today_jst.year)
                return dt.replace(tzinfo=TZ_JST)
            except ValueError:
                pass
        return None
    if isinstance(raw, (int, float)):
        epoch = datetime(1899, 12, 30, tzinfo=TZ_JST)  # Excel起点
        return epoch + timedelta(days=float(raw))
    if isinstance(raw, datetime):
        return raw.astimezone(TZ_JST) if raw.tzinfo else raw.replace(tzinfo=TZ_JST)
    return None

def chunk(lst: List[str], size: int) -> List[List[str]]:
    if size <= 0:
        return [lst]
    return [lst[i:i + size] for i in range(0, len(lst), size)]

# ===================== 認証 =====================
def build_gspread_client() -> gspread.Client:
    """
    gspreadクライアントを構築します。
    - GOOGLE_CREDENTIALS または GCP_SERVICE_ACCOUNT_KEY を優先
    - なければ credentials.json を使用
    """
    try:
        creds_str = os.environ.get("GOOGLE_CREDENTIALS")
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        if creds_str:
            info = json.loads(creds_str)
            credentials = ServiceAccountCredentials.from_json_keyfile_dict(info, scope)
            return gspread.authorize(credentials)
        else:
            creds_str_alt = os.environ.get("GCP_SERVICE_ACCOUNT_KEY")
            if creds_str_alt:
                credentials = json.loads(creds_str_alt)
            else:
                with open('credentials.json', 'r', encoding='utf-8') as f:
                    credentials = json.load(f)
            return gspread.service_account_from_dict(credentials)
    except Exception as e:
        raise RuntimeError(f"Google認証に失敗: {e}")

# ================= Yahooニュース リスト取得 =================
def get_yahoo_news_with_selenium(keyword: str) -> list[dict]:
    """
    SeleniumでYahoo!ニュース検索を開き、記事の タイトル/URL/投稿日/掲載元 を取得。
    """
    print(f"🚀 Yahoo!ニュース検索開始: {keyword}")
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--window-size=1280,1024")

    try:
        driver_path = ChromeDriverManager().install()
        service = Service(driver_path)
        driver = webdriver.Chrome(service=service, options=options)
    except Exception as e:
        print(f"❌ WebDriver初期化失敗: {e}")
        return []

    search_url = (
        f"https://news.yahoo.co.jp/search?p={keyword}"
        f"&ei=utf-8&categories=domestic,world,business,it,science,life,local"
    )
    driver.get(search_url)
    time.sleep(4)

    soup = BeautifulSoup(driver.page_source, "html.parser")
    driver.quit()

    articles = soup.find_all("li", class_=re.compile("sc-1u4589e-0"))
    articles_data = []

    for article in articles:
        try:
            # タイトル
            title_tag = article.find("div", class_=re.compile("sc-3ls169-0"))
            title = title_tag.text.strip() if title_tag else ""
            # URL
            link_tag = article.find("a", href=True)
            url = link_tag["href"] if link_tag else ""
            # 投稿日
            time_tag = article.find("time")
            date_str = time_tag.text.strip() if time_tag else ""
            formatted_date = ""
            if date_str:
                date_str = re.sub(r'\([月火水木金土日]\)', '', date_str).strip()
                try:
                    dt_obj = datetime.strptime(date_str, "%Y/%m/%d %H:%M")
                    formatted_date = format_datetime(dt_obj)
                except Exception:
                    formatted_date = date_str
            # 掲載元
            source_text = ""
            source_tag = article.find("div", class_="sc-n3vj8g-0 yoLqH")
            if source_tag:
                inner = source_tag.find("div", class_="sc-110wjhy-8 bsEjY")
                if inner and inner.span:
                    candidate = inner.span.text.strip()
                    if not candidate.isdigit():
                        source_text = candidate
            if not source_text or source_text.isdigit():
                alt_spans = article.find_all(["span", "div"], string=True)
                for s in alt_spans:
                    text = s.text.strip()
                    if 2 <= len(text) <= 20 and not text.isdigit() and re.search(r'[ぁ-んァ-ン一-龥A-Za-z]', text):
                        source_text = text
                        break

            if title and url:
                articles_data.append({
                    "タイトル": title,
                    "URL": url,
                    "投稿日": formatted_date if formatted_date else "取得不可",
                    "引用元": source_text
                })
        except Exception:
            continue

    print(f"✅ Yahoo!ニュース件数: {len(articles_data)} 件（{keyword}）")
    return articles_data

def write_news_list_to_source(gc: gspread.Client, articles: list[dict]) -> None:
    """
    取得した記事リストを SOURCE_SPREADSHEET の 'Yahoo' シートに追記（URL重複はスキップ）
    列: A=タイトル, B=URL, C=投稿日, D=引用元
    """
    for attempt in range(5):
        try:
            sh = gc.open_by_key(SOURCE_SPREADSHEET_ID)
            try:
                ws = sh.worksheet(SOURCE_SHEET_NAME)
            except gspread.exceptions.WorksheetNotFound:
                ws = sh.add_worksheet(title=SOURCE_SHEET_NAME, rows="1", cols="4")
                ws.append_row(['タイトル', 'URL', '投稿日', '引用元'])

            existing_data = ws.get_all_values()
            existing_urls = set(row[1] for row in existing_data[1:] if len(row) > 1)

            new_data = [[a['タイトル'], a['URL'], a['投稿日'], a['引用元']]
                        for a in articles if a['URL'] not in existing_urls]
            if new_data:
                ws.append_rows(new_data, value_input_option='USER_ENTERED')
                print(f"✅ SOURCEシートに {len(new_data)} 件追記")
            else:
                print("⚠️ 追記対象なし（すべて既存URL）")
            return
        except gspread.exceptions.APIError as e:
            print(f"⚠️ Google API Error (attempt {attempt + 1}/5): {e}")
            time.sleep(5 + random.random() * 5)
    raise RuntimeError("❌ SOURCEシートへの書き込みに失敗（5回再試行後）")

# ================= 本文・コメント取得 =================
def ensure_today_sheet(sh: gspread.Spreadsheet, tab_name: str) -> gspread.Worksheet:
    try:
        return sh.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=tab_name, rows="300", cols="30")

def get_existing_urls(ws: gspread.Worksheet) -> Set[str]:
    vals = ws.col_values(3)
    return set(vals[1:] if len(vals) > 1 else [])

def ensure_ae_header(ws: gspread.Worksheet) -> None:
    head = ws.row_values(1)
    target = ["ソース", "タイトル", "URL", "投稿日", "掲載元"]
    if head[:len(target)] != target:
        ws.update(range_name='A1', values=[target])

def ensure_body_comment_headers(ws: gspread.Worksheet, max_comment_pages: int) -> None:
    base = ["ソース", "タイトル", "URL", "投稿日", "掲載元"]
    body_headers = [f"本文({i}ページ)" for i in range(1, MAX_BODY_PAGES + 1)]  # F..O
    comments_count = ["コメント数"]  # P
    comment_page_headers = [f"コメント({i}ページJSON)" for i in range(1, max(1, max_comment_pages) + 1)]  # Q..
    target = base + body_headers + comments_count + comment_page_headers

    current = ws.row_values(1)
    if current != target:
        ws.update(range_name='A1', values=[target])

def transfer_a_to_e(gc: gspread.Client, dest_ws: gspread.Worksheet) -> int:
    """
    SOURCEシートから「前日15:00〜当日14:59:59」を当日タブに転送（A〜E）
    """
    sh_src = gc.open_by_key(SOURCE_SPREADSHEET_ID)
    ws_src = sh_src.worksheet(SOURCE_SHEET_NAME)
    rows = ws_src.get('A:D')

    now = jst_now()
    start = (now - timedelta(days=1)).replace(hour=15, minute=0, second=0, microsecond=0)
    end = now.replace(hour=14, minute=59, second=59, microsecond=0)

    ensure_ae_header(dest_ws)
    existing = get_existing_urls(dest_ws)

    to_append: List[List[str]] = []
    for i, r in enumerate(rows):
        if i == 0:
            continue
        title = r[0].strip() if len(r) > 0 and r[0] else ""
        url = r[1].strip() if len(r) > 1 and r[1] else ""
        posted_raw = r[2] if len(r) > 2 else ""
        site = r[3].strip() if len(r) > 3 and r[3] else ""
        if not title or not url:
            continue

        dt = parse_post_date(posted_raw, now)
        if not dt or not (start <= dt <= end):
            continue
        if url in existing:
            continue

        to_append.append(["Yahoo", title, url, format_yy_m_d_hm(dt), site])

    if to_append:
        dest_ws.append_rows(to_append, value_input_option="USER_ENTERED")
    return len(to_append)

def fetch_article_pages(base_url: str) -> Tuple[str, str, List[str]]:
    title = "取得不可"
    article_date = "取得不可"
    bodies: List[str] = []
    for page in range(1, MAX_BODY_PAGES + 1):
        url = base_url if page == 1 else f"{base_url}?page={page}"
        try:
            res = requests.get(url, headers=REQ_HEADERS, timeout=20)
            res.raise_for_status()
        except Exception:
            break
        soup = BeautifulSoup(res.text, "html.parser")

        if page == 1:
            t = soup.find("title")
            if t and t.get_text(strip=True):
                title = t.get_text(strip=True).replace(" - Yahoo!ニュース", "")
            time_tag = soup.find("time")
            if time_tag:
                article_date = time_tag.get_text(strip=True)

        body_text = ""
        article = soup.find("article")
        if article:
            ps = article.find_all("p")
            body_text = "\n".join(p.get_text(strip=True) for p in ps if p.get_text(strip=True))
        if not body_text:
            main = soup.find("main")
            if main:
                ps = main.find_all("p")
                body_text = "\n".join(p.get_text(strip=True) for p in ps if p.get_text(strip=True))

        if not body_text or (bodies and body_text == bodies[-1]):
            break
        bodies.append(body_text)
    return title, article_date, bodies

def fetch_comments_with_selenium(base_url: str) -> List[str]:
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,2000")

    try:
        driver_path = ChromeDriverManager().install()
        service = Service(driver_path)
        driver = webdriver.Chrome(service=service, options=options)
    except Exception as e:
        print(f"❌ WebDriver初期化エラー: {e}")
        return []

    comments: List[str] = []
    last_tail: Optional[str] = None
    page = 1
    try:
        while True:
            c_url = f"{base_url}/comments?page={page}"
            driver.get(c_url)
            time.sleep(2.0)

            soup = BeautifulSoup(driver.page_source, "html.parser")
            selectors = [
                "p.sc-169yn8p-10",
                "p[data-ylk*='cm_body']",
                "p[class*='comment']",
                "div.commentBody, p.commentBody",
                "div[data-ylk*='cm_body']"
            ]
            p_candidates = []
            for sel in selectors:
                p_candidates.extend(soup.select(sel))

            page_comments = [p.get_text(strip=True) for p in p_candidates if p.get_text(strip=True)]
            page_comments = list(dict.fromkeys(page_comments))

            if not page_comments:
                break

            if last_tail is not None and page_comments and page_comments[0] == last_tail:
                break

            comments.extend(page_comments)

            if len(comments) >= MAX_TOTAL_COMMENTS:
                comments = comments[:MAX_TOTAL_COMMENTS]
                break

            last_tail = page_comments[-1]
            page += 1
    finally:
        driver.quit()

    return comments

def write_bodies_and_comments(ws: gspread.Worksheet) -> None:
    urls = ws.col_values(3)[1:]  # C列（URL）
    total = len(urls)
    print(f"🔎 本文・コメント取得対象: {total} 件")
    if total == 0:
        return

    rows_data: List[List[str]] = []
    max_comment_pages = 0

    for idx, url in enumerate(urls, start=2):
        print(f"  - ({idx-1}/{total}) {url}")
        try:
            _title, _date, bodies = fetch_article_pages(url)
            comments = fetch_comments_with_selenium(url)

            body_cells = bodies[:MAX_BODY_PAGES] + [""] * (MAX_BODY_PAGES - len(bodies))

            comment_pages = chunk(comments, 10)
            json_per_page = [json.dumps(pg, ensure_ascii=False) for pg in comment_pages]
            cnt = len(comments)

            row = body_cells + [cnt] + json_per_page
            rows_data.append(row)

            if len(comment_pages) > max_comment_pages:
                max_comment_pages = len(comment_pages)
        except Exception as e:
            print(f"    ! Error: {e}")
            rows_data.append(([""] * MAX_BODY_PAGES) + [0])

    ensure_body_comment_headers(ws, max_comment_pages=max_comment_pages)

    need_cols = MAX_BODY_PAGES + 1 + max_comment_pages
    for i in range(len(rows_data)):
        if len(rows_data[i]) < need_cols:
            rows_data[i].extend([""] * (need_cols - len(rows_data[i])))

    if rows_data:
        ws.update(range_name="F2", values=rows_data)
        print(f"✅ 本文・コメント（JSON/ページ）書き込み完了: {len(rows_data)} 行（最大コメントページ={max_comment_pages}）")

# ================= メイン処理 =================
def main():
    gc = build_gspread_client()

    # 1) Yahoo!ニュース リスト取得（キーワードごとに収集→SOURCEへ）
    all_articles: list[dict] = []
    for kw in KEYWORDS:
        arts = get_yahoo_news_with_selenium(kw)
        all_articles.extend(arts)
        time.sleep(1.0)
    if not all_articles:
        print("💡 新規記事なし。終了。")
        return

    print(f"\n📄 Spreadsheet ID: {SHARED_SPREADSHEET_ID} / Sheet: {SOURCE_SHEET_NAME}")
    write_news_list_to_source(gc, all_articles)

    # 2) 当日タブ準備
    dest_sh = gc.open_by_key(DEST_SPREADSHEET_ID)
    today_tab = jst_now().strftime("%y%m%d")
    ws = ensure_today_sheet(dest_sh, today_tab)
    print(f"\n📄 DEST Sheet: {today_tab}")

    # 3) SOURCE -> DEST（A〜E）時間帯フィルタで転送
    added = transfer_a_to_e(gc, ws)
    print(f"📝 DESTへ新規追加: {added} 行")

    # 4) 本文・コメントをF列以降に書き込み
    if ws.get_all_values(value_render_option='UNFORMATTED_VALUE'):
        write_bodies_and_comments(ws)
    else:
        print("⚠️ 当日シートが空のため、本文・コメントの取得をスキップ")

if __name__ == "__main__":
    main()