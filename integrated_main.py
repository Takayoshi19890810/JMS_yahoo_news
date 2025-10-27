# -*- coding: utf-8 -*-
"""
Yahooニュース 自動収集スクリプト（JMS／Mobility Show 対応・1時間ごと実行）

機能概要：
- 検索キーワード：「JMS」「モビリティショー」「mobility show」
- 出力先シート：「Yahoo」（1枚で全履歴を管理）
- URL重複スキップ
- コメント最大5000件、1セルあたり50件(JSON配列)で分割記録（O〜Z列）
- 投稿日と取得日時は日本時間（JST）
"""

import os
import json
import time
import re
from datetime import datetime, timedelta, timezone
from typing import List, Tuple

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from bs4 import BeautifulSoup
import requests
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager


# ====== 設定 ======
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "1-oJl5lnTC2FRqayHsPW1ZVqfktAE99PCK6HGmKHiR28")
SHEET_NAME = "Yahoo"
KEYWORDS = ["JMS", "モビリティショー", "mobility show"]

MAX_BODY_PAGES = 10
MAX_TOTAL_COMMENTS = 5000
COMMENTS_PER_CELL = 50  # コメント1セルあたり最大件数
REQ_HEADERS = {"User-Agent": "Mozilla/5.0"}
TZ_JST = timezone(timedelta(hours=9))


# ====== 共通関数 ======
def jst_now() -> datetime:
    return datetime.now(TZ_JST)


def format_datetime(dt_obj) -> str:
    return dt_obj.strftime("%Y/%m/%d %H:%M")


def to_jst_from_str(raw: str) -> str:
    """Yahoo上の日付文字列をJST形式 'YYYY/MM/DD HH:MM' に統一"""
    if not raw:
        return "取得不可"
    raw = re.sub(r'\([月火水木金土日]\)', '', raw).strip()
    for fmt in ("%Y/%m/%d %H:%M", "%m/%d %H:%M"):
        try:
            dt = datetime.strptime(raw, fmt)
            if fmt == "%m/%d %H:%M":
                dt = dt.replace(year=jst_now().year)
            return format_datetime(dt)
        except ValueError:
            continue
    return raw


def chunk(lst: List[str], size: int) -> List[List[str]]:
    return [lst[i:i + size] for i in range(0, len(lst), size)]


# ====== Google認証 ======
def build_gspread_client() -> gspread.Client:
    creds_str = os.environ.get("GOOGLE_CREDENTIALS")
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    if creds_str:
        info = json.loads(creds_str)
        credentials = ServiceAccountCredentials.from_json_keyfile_dict(info, scope)
        return gspread.authorize(credentials)
    else:
        with open('credentials.json', 'r', encoding='utf-8') as f:
            credentials = json.load(f)
        return gspread.service_account_from_dict(credentials)


# ====== Yahooニュース検索 ======
def get_yahoo_news_with_selenium(keyword: str) -> list[dict]:
    print(f"🔎 検索中: {keyword}")
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1280,1024")

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    search_url = f"https://news.yahoo.co.jp/search?p={keyword}&ei=utf-8&categories=domestic,world,business,it,science,life,local"
    driver.get(search_url)
    time.sleep(3)
    soup = BeautifulSoup(driver.page_source, "html.parser")
    driver.quit()

    articles = soup.find_all("li", class_=re.compile("sc-1u4589e-0"))
    results = []
    for a in articles:
        try:
            title_tag = a.find("div", class_=re.compile("sc-3ls169-0"))
            title = title_tag.text.strip() if title_tag else ""
            link_tag = a.find("a", href=True)
            url = link_tag["href"] if link_tag else ""
            time_tag = a.find("time")
            date_str = to_jst_from_str(time_tag.text.strip() if time_tag else "取得不可")
            source_tag = a.find("div", class_="sc-n3vj8g-0 yoLqH")
            site = source_tag.text.strip() if source_tag else "取得不可"

            if title and url:
                results.append({"タイトル": title, "URL": url, "投稿日": date_str, "掲載元": site})
        except Exception:
            continue
    print(f"✅ {len(results)}件取得 ({keyword})")
    return results


# ====== 本文・コメント取得 ======
def fetch_article_pages(base_url: str) -> List[str]:
    bodies = []
    for page in range(1, MAX_BODY_PAGES + 1):
        url = base_url if page == 1 else f"{base_url}?page={page}"
        try:
            res = requests.get(url, headers=REQ_HEADERS, timeout=10)
            res.raise_for_status()
        except Exception:
            break
        soup = BeautifulSoup(res.text, "html.parser")
        article = soup.find("article") or soup.find("main")
        if not article:
            break
        ps = article.find_all("p")
        text = "\n".join(p.get_text(strip=True) for p in ps if p.get_text(strip=True))
        if not text or (bodies and text == bodies[-1]):
            break
        bodies.append(text)
    return bodies


def fetch_comments(base_url: str) -> List[List[str]]:
    """コメント最大5000件を取得し、50件単位で分割"""
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1280,2000")

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    comments = []
    page = 1
    try:
        while len(comments) < MAX_TOTAL_COMMENTS:
            c_url = f"{base_url}/comments?page={page}"
            driver.get(c_url)
            time.sleep(2)
            soup = BeautifulSoup(driver.page_source, "html.parser")
            elems = soup.select("p.sc-169yn8p-10, div.commentBody, p[data-ylk*='cm_body']")
            page_comments = [e.get_text(strip=True) for e in elems if e.get_text(strip=True)]
            if not page_comments:
                break
            comments.extend(page_comments)
            if len(page_comments) < 10:  # 最終ページ判定
                break
            page += 1
    finally:
        driver.quit()

    # 最大5000件に制限し、50件単位でチャンク化
    comments = comments[:MAX_TOTAL_COMMENTS]
    comment_cells = chunk(comments, COMMENTS_PER_CELL)
    return comment_cells


# ====== スプレッドシート ======
def ensure_yahoo_sheet(gc: gspread.Client):
    sh = gc.open_by_key(SPREADSHEET_ID)
    try:
        ws = sh.worksheet(SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=SHEET_NAME, rows="1000", cols="100")
        ws.append_row(
            ["ソース", "タイトル", "URL", "投稿日", "掲載元", "取得日時"]
            + [f"本文({i}ページ)" for i in range(1, MAX_BODY_PAGES + 1)]
            + ["コメント数"]
            + [f"コメント({i*50-49}〜{i*50})" for i in range(1, 101)]
        )
    return ws


def append_to_sheet(ws, data: List[List[str]]):
    if data:
        ws.append_rows(data, value_input_option="USER_ENTERED")
        print(f"📝 {len(data)}行追加しました。")


# ====== メイン処理 ======
def main():
    gc = build_gspread_client()
    ws = ensure_yahoo_sheet(gc)
    existing_urls = set(ws.col_values(3)[1:])  # URL列(C)

    all_articles = []
    for kw in KEYWORDS:
        all_articles.extend(get_yahoo_news_with_selenium(kw))
        time.sleep(1)

    new_rows = []
    for art in all_articles:
        url = art["URL"]
        if url in existing_urls:
            continue

        title = art["タイトル"]
        date = art["投稿日"]
        site = art["掲載元"]
        timestamp = format_datetime(jst_now())

        bodies = fetch_article_pages(url)
        comment_cells = fetch_comments(url)
        comment_jsons = [json.dumps(pg, ensure_ascii=False) for pg in comment_cells]
        comment_count = sum(len(pg) for pg in comment_cells)

        row = (
            ["Yahoo", title, url, date, site, timestamp]
            + bodies[:MAX_BODY_PAGES] + [""] * (MAX_BODY_PAGES - len(bodies))
            + [comment_count] + comment_jsons
        )
        new_rows.append(row)

    if new_rows:
        append_to_sheet(ws, new_rows)
    else:
        print("⚠️ 新規記事なし。")


if __name__ == "__main__":
    main()
