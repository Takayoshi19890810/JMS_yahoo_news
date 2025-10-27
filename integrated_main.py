# -*- coding: utf-8 -*-
"""
Yahooニュース 自動収集統合スクリプト（JMS／Mobility Show 対応・1時間ごと実行）

特徴：
- キーワード：「JMS」「モビリティショー」「mobility show」
- 出力先シートは1枚（"Yahoo"）のみ
- URL重複はスキップ
- 本文とコメントを同時に取得して追記
- Google Sheets APIはサービスアカウント認証
"""

import os
import json
import time
import re
import random
from datetime import datetime, timedelta, timezone
from typing import List, Optional

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
REQ_HEADERS = {"User-Agent": "Mozilla/5.0"}
TZ_JST = timezone(timedelta(hours=9))


# ====== 共通関数 ======
def jst_now() -> datetime:
    return datetime.now(TZ_JST)


def format_datetime(dt_obj) -> str:
    return dt_obj.strftime("%Y/%m/%d %H:%M")


def chunk(lst: List[str], size: int) -> List[List[str]]:
    if size <= 0:
        return [lst]
    return [lst[i:i + size] for i in range(0, len(lst), size)]


# ====== Google認証 ======
def build_gspread_client() -> gspread.Client:
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
        raise RuntimeError(f"Google認証失敗: {e}")


# ====== Yahooニュース検索 ======
def get_yahoo_news_with_selenium(keyword: str) -> list[dict]:
    print(f"🚀 Yahoo!ニュース検索中: {keyword}")
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1280,1024")

    try:
        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    except Exception as e:
        print(f"❌ WebDriver初期化エラー: {e}")
        return []

    search_url = f"https://news.yahoo.co.jp/search?p={keyword}&ei=utf-8&categories=domestic,world,business,it,science,life,local"
    driver.get(search_url)
    time.sleep(4)
    soup = BeautifulSoup(driver.page_source, "html.parser")
    driver.quit()

    articles = soup.find_all("li", class_=re.compile("sc-1u4589e-0"))
    results = []

    for article in articles:
        try:
            title_tag = article.find("div", class_=re.compile("sc-3ls169-0"))
            title = title_tag.text.strip() if title_tag else ""
            link_tag = article.find("a", href=True)
            url = link_tag["href"] if link_tag else ""
            time_tag = article.find("time")
            date_str = time_tag.text.strip() if time_tag else "取得不可"
            source = article.find("div", class_="sc-n3vj8g-0 yoLqH")
            site = source.text.strip() if source else "取得不可"

            if title and url:
                results.append({"タイトル": title, "URL": url, "投稿日": date_str, "掲載元": site})
        except Exception:
            continue
    print(f"✅ 取得件数({keyword}): {len(results)}")
    return results


# ====== 本文・コメント取得 ======
def fetch_article_pages(base_url: str) -> List[str]:
    """本文(最大10ページ)"""
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
        body = "\n".join(p.get_text(strip=True) for p in ps if p.get_text(strip=True))
        if not body or (bodies and body == bodies[-1]):
            break
        bodies.append(body)
    return bodies


def fetch_comments(base_url: str) -> List[str]:
    """コメント全取得"""
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1280,2000")

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
    comments = []
    page = 1
    try:
        while True:
            c_url = f"{base_url}/comments?page={page}"
            driver.get(c_url)
            time.sleep(2)
            soup = BeautifulSoup(driver.page_source, "html.parser")
            elems = soup.select("p.sc-169yn8p-10, div.commentBody, p[data-ylk*='cm_body']")
            page_comments = [e.get_text(strip=True) for e in elems if e.get_text(strip=True)]
            page_comments = list(dict.fromkeys(page_comments))
            if not page_comments:
                break
            comments.extend(page_comments)
            if len(comments) >= MAX_TOTAL_COMMENTS:
                comments = comments[:MAX_TOTAL_COMMENTS]
                break
            page += 1
    finally:
        driver.quit()
    return comments


# ====== スプレッドシート出力 ======
def ensure_yahoo_sheet(gc: gspread.Client):
    sh = gc.open_by_key(SPREADSHEET_ID)
    try:
        ws = sh.worksheet(SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=SHEET_NAME, rows="1000", cols="30")
        ws.append_row([
            "ソース", "タイトル", "URL", "投稿日", "掲載元",
            *[f"本文({i}ページ)" for i in range(1, MAX_BODY_PAGES + 1)],
            "コメント数", "コメント(JSON)", "取得日時"
        ])
    return ws


def append_to_sheet(ws, data: List[List[str]]):
    if data:
        ws.append_rows(data, value_input_option="USER_ENTERED")
        print(f"📝 {len(data)} 行を追記しました。")


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

        bodies = fetch_article_pages(url)
        comments = fetch_comments(url)
        comment_pages = chunk(comments, 10)
        json_str = json.dumps(comment_pages, ensure_ascii=False)
        now_str = format_datetime(jst_now())

        row = ["Yahoo", title, url, date, site] + \
              bodies[:MAX_BODY_PAGES] + [""] * (MAX_BODY_PAGES - len(bodies)) + \
              [len(comments), json_str, now_str]
        new_rows.append(row)

    if new_rows:
        append_to_sheet(ws, new_rows)
    else:
        print("⚠️ 新規記事なし。")


if __name__ == "__main__":
    main()
