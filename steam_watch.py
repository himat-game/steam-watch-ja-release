#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
steam_watch.py  (lightweight / stdlib-only)

機能:
  - Steam全アプリを少しずつ巡回し、以下を検知してRSSに出力
    ① supported_languages に日本語が新規に含まれた（JA追加検知）
    ② release_date.date の追加 or 変更（発売日追加/変更検知）
  - 画像: <media:thumbnail> と <description> 内 <img> を同梱（多くのRSSリーダーでサムネ表示）
  - 依存: 標準ライブラリのみ（requests等は不使用）
  - 状態: state.json.gz に保存（GitHub Actions では Cache/Artifact で扱う想定）
  - 失敗時でも finally で必ず state を保存、初回起動時は空でも state.json.gz を作成
"""

import argparse
import json
import gzip
import os
import time
import html
import datetime
from urllib.request import urlopen, Request
from urllib.parse import urlencode

APP_LIST_URL = "https://api.steampowered.com/ISteamApps/GetAppList/v2/"
APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
UA = "Mozilla/5.0 (compatible; steam-watch/1.0)"

# -------------------------
# HTTP / STATE I/O
# -------------------------

def http_get(url, params=None, timeout=30):
    if params:
        url = f"{url}?{urlencode(params)}"
    req = Request(url, headers={"User-Agent": UA})
    with urlopen(req, timeout=timeout) as r:
        return r.read()

def load_state(path):
    if os.path.isfile(path):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_state(path, state):
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)

# -------------------------
# UTIL
# -------------------------

def now_rfc2822():
    return datetime.datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")

def ensure_applist(state, max_age_days=7):
    """applistが無い/古いなら更新し、cursorを準備"""
    ts = state.get("applist_ts", 0)
    age_days = (time.time() - ts) / 86400 if ts else 1e9
    if "applist" not in state or age_days > max_age_days:
        raw = http_get(APP_LIST_URL)
        data = json.loads(raw)
        apps = data["applist"]["apps"]
        state["applist"] = [a["appid"] for a in apps if "appid" in a]
        state["applist_ts"] = time.time()
    if "cursor" not in state:
        state["cursor"] = 0

def fetch_details(appid, lang="en", cc="us"):
    """単発appdetails（最小・安定運用）"""
    params = {"appids": str(appid), "l": lang, "cc": cc}
    raw = http_get(APPDETAILS_URL, params=params)
    j = json.loads(raw)
    key = str(appid)
    if key not in j or not j[key].get("success"):
        return None
    return j[key].get("data") or {}

def has_japanese(supported_languages: str) -> bool:
    if not supported_languages:
        return False
    text = html.unescape(supported_languages)
    return ("japanese" in text.lower()) or ("日本語" in text)

def normalize_date(date_str: str) -> str:
    if not date_str:
        return ""
    # 文字列比較のためそのまま格納（地域表記差は無理にパースしない）
    return date_str.strip()

def escape_xml(s):
    return (s or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

def update_rss(rss_items, title, link_self, max_items=200):
    """
    RSS生成：
      - Media RSS 名前空間（<media:thumbnail>）を付与
      - 互換のため <description> にも <img> を先頭に同梱
    """
    items_xml = []
    for it in rss_items[:max_items]:
        desc = it.get("description","") or ""
        img = it.get("image")
        if img:
            # 一部リーダー対策で description 先頭に img を埋め込む
            desc = f'<p><img src="{escape_xml(img)}" referrerpolicy="no-referrer" loading="lazy" /></p>' + desc

        media_thumb = f'\n    <media:thumbnail url="{escape_xml(img)}" />' if img else ""

        items_xml.append(
f"""  <item>
    <title>{escape_xml(it["title"])}</title>
    <link>{escape_xml(it["link"])}</link>
    <guid>{escape_xml(it["guid"])}</guid>
    <pubDate>{escape_xml(it["pubDate"])}</pubDate>{media_thumb}
    <description>{escape_xml(desc)}</description>
  </item>"""
        )

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
<channel>
  <title>{escape_xml(title)}</title>
  <link>{escape_xml(link_self)}</link>
  <description>Steam watch feed</description>
  <language>en</language>
  <lastBuildDate>{escape_xml(now_rfc2822())}</lastBuildDate>
{os.linesep.join(items_xml)}
</channel>
</rss>
"""
    return xml

# -------------------------
# MAIN
# -------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="state.json.gz")
    ap.add_argument("--batch-size", type=int, default=250)
    ap.add_argument("--max-rss", type=int, default=200)
    ap.add_argument("--sleep-ms", type=int, default=200, help="appdetails間の待機(ms)")
    args = ap.parse_args()

    # 実行ディレクトリを表示（デバッグ）
    print(f"[cwd] {os.getcwd()}")

    state = load_state(args.state)

    # RSSバッファ（先頭が最新）
    state.setdefault("rss_lang", [])
    state.setdefault("rss_release", [])
    state.setdefault("known", {})  # appid -> {"has_ja": bool, "release": str}

    ensure_applist(state)

    # ★初回でも必ず state.json.gz を作る（空でも一度保存）
    if not os.path.isfile(args.state):
        save_state(args.state, state)
        print("[state] created initial state file")

    apps = state["applist"]
    n = len(apps)
    cursor = state.get("cursor", 0)

    checked = 0
    found_lang = 0
    found_rel = 0

    try:
        for i in range(args.batch_size):
            if n == 0:
                break
            idx = (cursor + i) % n
            appid = apps[idx]

            try:
                data = fetch_details(appid)
            except Exception:
                # 軽いバックオフ
                time.sleep(1.0)
                continue

            if not data:
                time.sleep(args.sleep_ms / 1000)
                continue

            name = data.get("name") or f"App {appid}"
            sl = data.get("supported_languages") or ""
            rd = data.get("release_date") or {}
            rd_date = normalize_date(rd.get("date") or "")

            # 画像URLの決定（存在優先で採用）
            img_url = (
                data.get("capsule_imagev5")
                or data.get("capsule_image")
                or data.get("header_image")
                or f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{appid}/capsule_231x87.jpg"
            )

            prev = state["known"].get(str(appid), {"has_ja": False, "release": ""})
            now_has_ja = has_japanese(sl)
            now_rel = rd_date

            # ① 日本語 追加検知（False -> True）
            if (not prev.get("has_ja")) and now_has_ja:
                item = {
                    "title": f"[JA added] {name}",
                    "link": f"https://store.steampowered.com/app/{appid}/",
                    "guid": f"ja-{appid}-{int(time.time())}",
                    "pubDate": now_rfc2822(),
                    "description": "Japanese language appeared in supported_languages.",
                    "image": img_url,
                }
                state["rss_lang"].insert(0, item)
                found_lang += 1

            # ② 発売日 追加／変更検知（"" -> X もしくは A -> B）
            if (prev.get("release", "") != now_rel):
                kind = "Release date added" if prev.get("release", "") == "" and now_rel != "" else "Release date changed"
                item = {
                    "title": f"[{kind}] {name} -> {now_rel or '(blank)'}",
                    "link": f"https://store.steampowered.com/app/{appid}/",
                    "guid": f"rel-{appid}-{int(time.time())}",
                    "pubDate": now_rfc2822(),
                    "description": f"release_date.date: '{prev.get('release','')}' -> '{now_rel}'",
                    "image": img_url,
                }
                state["rss_release"].insert(0, item)
                found_rel += 1

            # 状態更新
            state["known"][str(appid)] = {
                "has_ja": bool(now_has_ja),
                "release": now_rel,
            }

            checked += 1
            time.sleep(args.sleep_ms / 1000)

        # カーソル前進
        state["cursor"] = (cursor + checked) % (n or 1)

        # RSS出力（上限）
        rss_lang_xml = update_rss(
            state["rss_lang"],
            "Steam: Japanese Language Added",
            "https://example.invalid/rss_lang_ja_added.xml",
            args.max_rss,
        )
        rss_rel_xml = update_rss(
            state["rss_release"],
            "Steam: Release Date Added/Changed",
            "https://example.invalid/rss_release_changed.xml",
            args.max_rss,
        )
        with open("rss_lang_ja_added.xml", "w", encoding="utf-8") as f:
            f.write(rss_lang_xml)
        with open("rss_release_changed.xml", "w", encoding="utf-8") as f:
            f.write(rss_rel_xml)

    finally:
        # ★必ず保存（途中で例外が起きても state は残す）
        save_state(args.state, state)
        print(f"[state] saved: {args.state}")
        print(f"checked={checked} cursor={state.get('cursor','?')}/{n} ja_added={found_lang} rel_changes={found_rel}")

if __name__ == "__main__":
    main()
