"""
イベント関連企業リサーチ・絞り込みツール v3
完全自動イベントリサーチ ＋ MC・ナレーター需要企業の絞り込み

変更点(v3):
- STEP1をジャンル選択→全自動Googleリサーチ→Gemini整理に変更
- イベント名をすべての出力カラムに追加（列2: AIが自動リサーチしたイベント名）
- イベント段階では除外フィルタを適用しない（企業段階のみ）
"""

import streamlit as st
import requests
from bs4 import BeautifulSoup
import google.generativeai as genai
import gspread
from google.oauth2.service_account import Credentials
import pandas as pd
import time
import json
import re
from urllib.parse import urljoin, urlparse
from datetime import datetime
import os
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# 定数・設定
# ============================================================

EXCLUSION_DOMAINS: list[str] = [
    "l-amitie.co.jp",
    "fairy1990.com",
]

EXCLUSION_KEYWORDS: list[str] = [
    "エル・アミティエ", "エルアミティエ", "l-amitie",
    "フェアリィ", "fairy1990",
]

GEMINI_MODEL = "gemini-1.5-flash"

GS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

TOKYO_AREA_HINT = (
    "東京都各区（港区・渋谷区・新宿区・千代田区・中央区・品川区・豊島区・中野区・"
    "目黒区・世田谷区・江東区・台東区・墨田区・荒川区・足立区・葛飾区・江戸川区・"
    "板橋区・練馬区・杉並区・北区・文京区など）、"
    "神奈川県（横浜市・川崎市・武蔵小杉など東京寄り）、"
    "埼玉県（さいたま市・大宮・浦和など）、"
    "千葉県（千葉市・船橋市・海浜幕張・市川・松戸・柏など東京寄り）"
)

# 出力列定義（順番厳守）
OUTPUT_COLUMNS = [
    "イベントジャンル（50選）",
    "AIが自動リサーチしたイベント名",
    "企業名",
    "企業のHP URL",
    "連絡先（代表メールや問い合わせ窓口）",
    "担当者肩書",
    "担当者名",
    "イベント開催時期",
    "開催会場",
    "イベント形式",
    "ジャンルや特徴・詳細",
    "ステージの種類・MC業務内容",
    "MCの想定シーン",
    "情報ソース",
    "書き込み日時",
]

MAX_SEARCH_RESULTS_PER_QUERY = 10

# ============================================================
# イベントジャンル一覧（50カテゴリ）
# ============================================================
GENRE_LIST: list[dict] = [
    {
        "label": "1. IT・DX・AI・テクノロジー",
        "keywords": ["IT展示会", "DX イベント", "AI テクノロジー 展示会",
                     "クラウド セキュリティ 展示会", "アプリ開発 イベント"],
    },
    {
        "label": "2. 医療・医薬品・バイオ",
        "keywords": ["医療 展示会", "医薬品 バイオ イベント",
                     "新薬開発 展示会", "ライフサイエンス 展示会", "製薬 展示会"],
    },
    {
        "label": "3. 看護・介護・福祉",
        "keywords": ["介護 展示会", "看護 福祉 イベント",
                     "介護ロボット 展示会", "福祉用具 展示会", "ケア 展示会"],
    },
    {
        "label": "4. 病院・クリニック運営",
        "keywords": ["医療機器 展示会", "病院設備 展示会",
                     "電子カルテ 展示会", "クリニック 医療IT 展示会"],
    },
    {
        "label": "5. 製造業・ファクトリーオートメーション（FA）",
        "keywords": ["製造業 展示会", "FA ファクトリーオートメーション 展示会",
                     "工作機械 展示会", "産業用ロボット 展示会", "3Dプリンタ 展示会"],
    },
    {
        "label": "6. 半導体・電子部品・ディスプレイ",
        "keywords": ["半導体 展示会", "電子部品 展示会",
                     "ディスプレイ 展示会", "センサ 展示会", "回路設計 イベント"],
    },
    {
        "label": "7. 自動車・モビリティ",
        "keywords": ["自動車 展示会", "EV 電気自動車 展示会",
                     "自動運転 展示会", "モビリティ 展示会", "カーテクノロジー"],
    },
    {
        "label": "8. 物流・ロジスティクス・マテハン",
        "keywords": ["物流 展示会", "ロジスティクス 展示会",
                     "マテハン 展示会", "倉庫管理 自動搬送 展示会", "梱包 展示会"],
    },
    {
        "label": "9. 食品・飲料・醸造",
        "keywords": ["食品 展示会", "飲料 醸造 展示会",
                     "食材 食品加工 展示会", "食品衛生 パッケージ 展示会"],
    },
    {
        "label": "10. 外食・フードサービス・店舗運営",
        "keywords": ["外食 展示会", "フードサービス 展示会",
                     "厨房機器 展示会", "飲食店 POSレジ 展示会", "フードビジネス"],
    },
    {
        "label": "11. 建築・建設・住宅",
        "keywords": ["建築 建設 展示会", "住宅 展示会",
                     "建材 展示会", "スマートハウス 展示会", "施工技術 展示会"],
    },
    {
        "label": "12. 不動産・マンション管理",
        "keywords": ["不動産 展示会", "マンション管理 展示会",
                     "プロパティマネジメント 展示会", "不動産テック 展示会"],
    },
    {
        "label": "13. 環境・省エネ・グリーンテクノロジー",
        "keywords": ["環境 展示会", "省エネ グリーンテクノロジー 展示会",
                     "脱炭素 展示会", "リサイクル 水処理 展示会"],
    },
    {
        "label": "14. 水素・燃料電池・二次電池",
        "keywords": ["水素 展示会", "燃料電池 展示会",
                     "二次電池 蓄電池 展示会", "スマートグリッド 展示会", "次世代エネルギー"],
    },
    {
        "label": "15. 小売・流通・EC・マーケティング",
        "keywords": ["小売 流通 展示会", "EC 通販 展示会",
                     "マーケティング 展示会", "店舗販促 集客 展示会"],
    },
    {
        "label": "16. 総務・人事・経理・法務",
        "keywords": ["総務 人事 展示会", "経理 法務 展示会",
                     "働き方改革 福利厚生 展示会", "オフィス設備 展示会"],
    },
    {
        "label": "17. 観光・旅行・ホテル・インバウンド",
        "keywords": ["観光 旅行 展示会", "ホテル インバウンド 展示会",
                     "旅行テック 展示会", "地域活性 観光 展示会"],
    },
    {
        "label": "18. 美容・コスメ・健康・サロン運営",
        "keywords": ["美容 コスメ 展示会", "健康 サロン 展示会",
                     "化粧品 エステ 展示会", "サプリメント ウェルネス 展示会"],
    },
    {
        "label": "19. ファッション・アパレル・テキスタイル",
        "keywords": ["ファッション アパレル 展示会", "テキスタイル 展示会",
                     "OEM 服飾 展示会", "衣服 生地 展示会"],
    },
    {
        "label": "20. インテリア・家具・空間デザイン",
        "keywords": ["インテリア 家具 展示会", "空間デザイン 展示会",
                     "オフィス家具 照明 展示会", "店舗デザイン 展示会"],
    },
    {
        "label": "21. 玩具・ホビー・ゲーム・エンタメ",
        "keywords": ["玩具 ホビー 展示会", "ゲーム エンタメ 展示会",
                     "フィギュア キャラクターグッズ 展示会", "ゲーム開発 展示会"],
    },
    {
        "label": "22. 教育・EdTech・学校設立",
        "keywords": ["教育 EdTech 展示会", "eラーニング 展示会",
                     "学校設備 教材 展示会", "塾 教育サービス 展示会"],
    },
    {
        "label": "23. スポーツ・フィットネス・ウェルネス",
        "keywords": ["スポーツ フィットネス 展示会", "ウェルネス 展示会",
                     "トレーニングマシン スポーツ用品 展示会"],
    },
    {
        "label": "24. 文具・紙製品・オフィスサプライ",
        "keywords": ["文具 紙製品 展示会", "オフィスサプライ 展示会",
                     "事務用品 ギフト 展示会", "高級筆記具 展示会"],
    },
    {
        "label": "25. ブライダル・ジュエリー・時計",
        "keywords": ["ブライダル 展示会", "ジュエリー 時計 展示会",
                     "結婚式 ウェディング 展示会", "宝飾品 展示会"],
    },
    {
        "label": "26. ペット・動物・トリミング",
        "keywords": ["ペット 展示会", "動物 トリミング 展示会",
                     "ペットフード ケア用品 展示会", "ペットビジネス 展示会"],
    },
    {
        "label": "27. 農業・スマート農業・園芸",
        "keywords": ["農業 展示会", "スマート農業 展示会",
                     "農業機械 ドローン 展示会", "園芸 ビニールハウス 展示会"],
    },
    {
        "label": "28. 宇宙・航空・防衛・海洋",
        "keywords": ["宇宙 航空 展示会", "防衛 海洋 展示会",
                     "航空宇宙技術 展示会", "ドローン 宇宙開発 展示会"],
    },
    {
        "label": "29. 自治体・公共サービス・防災",
        "keywords": ["自治体 公共サービス 展示会", "防災 展示会",
                     "スマートシティ 展示会", "防犯 災害対策 展示会"],
    },
    {
        "label": "30. 金融・フィンテック・資産運用",
        "keywords": ["金融 フィンテック 展示会", "資産運用 展示会",
                     "投資 銀行 展示会", "金融セキュリティ 展示会"],
    },
    {
        "label": "31. メタバース・Web3・XR",
        "keywords": ["メタバース 展示会", "Web3 ブロックチェーン 展示会",
                     "VR AR XR 展示会", "バーチャル空間 展示会"],
    },
    {
        "label": "32. 動画制作・映像・ライブ配信技術",
        "keywords": ["映像 動画制作 展示会", "ライブ配信 展示会",
                     "業務用カメラ 音声機材 展示会", "配信システム 展示会"],
    },
    {
        "label": "33. 広告・プロモーション・PR",
        "keywords": ["広告 プロモーション 展示会", "PR 展示会",
                     "屋外広告 販促 展示会", "サンプリング イベントマーケティング"],
    },
    {
        "label": "34. デジタルマーケティング・SNS・SEO",
        "keywords": ["デジタルマーケティング 展示会", "SNS マーケティング 展示会",
                     "SEO インフルエンサー 展示会", "データ分析 マーケティング 展示会"],
    },
    {
        "label": "35. 商業施設・店舗開発・サイネージ",
        "keywords": ["商業施設 店舗開発 展示会", "サイネージ 展示会",
                     "デジタルサイネージ 看板 展示会", "什器 ディスプレイ 展示会"],
    },
    {
        "label": "36. 通信インフラ・次世代通信・5G/6G",
        "keywords": ["通信インフラ 展示会", "5G 6G 展示会",
                     "ネットワーク IoT 展示会", "次世代通信 展示会"],
    },
    {
        "label": "37. 人材採用・就職支援・HRテック",
        "keywords": ["人材採用 展示会", "HRテック 展示会",
                     "就職支援 求人 展示会", "採用代行 適性検査 展示会"],
    },
    {
        "label": "38. フランチャイズ（FC）・起業・独立支援",
        "keywords": ["フランチャイズ 展示会", "起業 独立支援 展示会",
                     "FC加盟 代理店 展示会", "創業 ビジネスマッチング 展示会"],
    },
    {
        "label": "39. コンテンツ・アニメ・ライセンス",
        "keywords": ["コンテンツ アニメ 展示会", "ライセンス 展示会",
                     "キャラクタービジネス IP 展示会", "著作権 コンテンツビジネス"],
    },
    {
        "label": "40. 出版・メディア・電子書籍",
        "keywords": ["出版 メディア 展示会", "電子書籍 展示会",
                     "印刷 編集 展示会", "書籍 出版ビジネス 展示会"],
    },
    {
        "label": "41. 印刷・包装・業務用パッケージ",
        "keywords": ["印刷 包装 展示会", "パッケージ 展示会",
                     "特殊印刷 ラベル 展示会", "梱包機械 展示会"],
    },
    {
        "label": "42. 素材・化学・高機能マテリアル",
        "keywords": ["素材 化学 展示会", "高機能マテリアル 展示会",
                     "プラスチック 炭素繊維 展示会", "ナノテク 機能材料 展示会"],
    },
    {
        "label": "43. 清掃・ビルメンテナンス・施設管理",
        "keywords": ["清掃 ビルメンテナンス 展示会", "施設管理 展示会",
                     "業務用清掃機 展示会", "害虫駆除 設備保守 展示会"],
    },
    {
        "label": "44. 防犯・災害対策・セキュリティ",
        "keywords": ["防犯 セキュリティ 展示会", "災害対策 展示会",
                     "防犯カメラ 生体認証 展示会", "入退室管理 展示会"],
    },
    {
        "label": "45. 航空・空港・鉄道・交通インフラ",
        "keywords": ["航空 空港 展示会", "鉄道 交通インフラ 展示会",
                     "運行システム 駅設備 展示会", "機内サービス 航空設備 展示会"],
    },
    {
        "label": "46. ライフスタイル・生活雑貨・ガジェット",
        "keywords": ["ライフスタイル 生活雑貨 展示会", "ガジェット 展示会",
                     "アイデア商品 便利グッズ 展示会", "ギフト 雑貨 展示会"],
    },
    {
        "label": "47. アウトドア・キャンプ・キャンピングカー",
        "keywords": ["アウトドア キャンプ 展示会", "キャンピングカー RV 展示会",
                     "アウトドア用品 展示会", "サバイバルギア 展示会"],
    },
    {
        "label": "48. マタニティ・ベビー・キッズビジネス",
        "keywords": ["マタニティ ベビー 展示会", "キッズビジネス 展示会",
                     "育児用品 知育玩具 展示会", "子供向けサービス 展示会"],
    },
    {
        "label": "49. 伝統工芸・地方創生・地域特産品",
        "keywords": ["伝統工芸 展示会", "地方創生 地域特産品 展示会",
                     "お土産ビジネス 展示会", "自治体PR 地域活性 展示会"],
    },
    {
        "label": "50. ライフエンディング・葬祭・終活",
        "keywords": ["葬祭 展示会", "終活 ライフエンディング 展示会",
                     "葬儀設備 墓石 展示会", "終活サポート 展示会"],
    },
]

GENRE_LABELS: list[str] = ["― ジャンルを選択してください ―"] + [g["label"] for g in GENRE_LIST]


# ============================================================
# セッションステートの初期化
# ============================================================

def init_session_state() -> None:
    defaults: dict = {
        "events": [],                    # 自動リサーチで得たイベントリスト
        "selected_event": None,          # 選択中のイベント dict
        "selected_genre_label": "不明",  # 選択中ジャンル名
        "companies": [],                 # 収集済み企業リスト
        "excluded_companies": [],        # 除外された企業リスト
        "ai_results": [],                # Gemini判定全結果
        "filtered_companies": [],        # 最終絞り込み後リスト
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


# ============================================================
# JSON安全パーサー（オブジェクト・配列の両方に対応）
# ============================================================

def _extract_json(text: str) -> "dict | list | None":
    """
    Geminiレスポンスから JSON オブジェクト or 配列を安全に抽出する。
    マークダウンのコードブロック混入にも対応。
    """
    # ``` ブロック内を優先探索
    md_match = re.search(r"```(?:json)?\s*([\[{].*?[\]}])\s*```", text, re.DOTALL)
    if md_match:
        try:
            return json.loads(md_match.group(1))
        except json.JSONDecodeError:
            pass

    # JSON配列 [ ... ]
    arr_match = re.search(r"\[[\s\S]*\]", text)
    if arr_match:
        try:
            return json.loads(arr_match.group(0))
        except json.JSONDecodeError:
            pass

    # JSON オブジェクト { ... }
    obj_match = re.search(r"\{[\s\S]*\}", text)
    if obj_match:
        try:
            return json.loads(obj_match.group(0))
        except json.JSONDecodeError:
            pass

    return None


# ============================================================
# 検索バックエンド: SerpAPI（Google検索結果を取得）
# ============================================================

def _serpapi_search(query: str, api_key: str, num: int = 10) -> list[dict]:
    """
    SerpAPI 経由でGoogle検索を実行し、organic_results を返す。

    Returns:
        list of result dicts（各要素に title / link / snippet を含む）
    """
    try:
        resp = requests.get(
            "https://serpapi.com/search.json",
            params={
                "q": query,
                "api_key": api_key,
                "engine": "google",
                "hl": "ja",
                "gl": "jp",
                "num": num,
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            st.warning(f"SerpAPIエラー: {data['error']}")
            return []
        return data.get("organic_results", [])
    except Exception as exc:
        st.warning(f"SerpAPI検索エラー（{query[:20]}）: {exc}")
        return []


# ============================================================
# STEP 1: 完全自動イベントリサーチ
# ============================================================

def auto_research_events(
    genre: dict,
    serpapi_key: str,
    max_queries: int = 3,
) -> list[dict]:
    """
    選択ジャンルのキーワードを使ってSerpAPI（Google検索）で検索し、
    イベント候補（タイトル・URL・スニペット）を収集する。

    Args:
        max_queries: 使用するキーワード数の上限（APIクォータ節約）

    Returns:
        list of {title, url, snippet}
    """
    candidates: list[dict] = []
    seen_urls: set[str] = set()

    keywords = genre.get("keywords", [])[:max_queries]

    for kw in keywords:
        # 東京近郊 + 展示会/セミナー/講演会 を付加して絞り込む
        query = (
            f"{kw} 東京 OR 幕張 OR 横浜 OR 川崎 OR さいたま OR 千葉 "
            f"展示会 OR セミナー OR 講演会 OR カンファレンス OR 自社イベント "
            f"2025 OR 2026"
        )
        results = _serpapi_search(query, serpapi_key, MAX_SEARCH_RESULTS_PER_QUERY)
        for item in results:
            url = item.get("link", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                candidates.append({
                    "title": item.get("title", ""),
                    "url": url,
                    "snippet": item.get("snippet", ""),
                })
        time.sleep(0.4)  # API連続呼び出し間隔

    return candidates


def validate_events_with_gemini(
    candidates: list[dict],
    genre_label: str,
    gemini_api_key: str,
) -> list[dict]:
    """
    Google検索で得たイベント候補をGeminiに一括送信し、
    実際の東京近郊イベントを整理・抽出したリストを返す。
    1回のAPI呼び出しで完結（レートリミット対策）。

    Returns:
        list of {name, url, venue, timing, format, snippet}
    """
    if not candidates:
        return []

    # Geminiに渡す候補テキストを作成（最大20件）
    items_text = "\n".join([
        f"[{i + 1}] タイトル: {c['title']}\n    URL: {c['url']}\n    概要: {c['snippet']}"
        for i, c in enumerate(candidates[:20])
    ])

    prompt = f"""あなたは日本のイベントリサーチ専門家です。
以下のGoogle検索結果から、【{genre_label}】に関連する「展示会・見本市・セミナー・講演会・カンファレンス・配信イベント」を特定し、
東京都内または東京近郊（神奈川・千葉・埼玉の東京寄りエリア）で開催される（または開催された）イベントのみを抽出してください。

【検索結果】
{items_text}

【抽出ルール】
- 企業の製品ページ・トップページ・ニュース記事など、イベント自体でないURLは除外
- 過去・今後どちらのイベントも含める
- MCやナレーターが活躍するステージ・ブース・司会進行がありそうなイベントを優先
- エル・アミティエ・フェアリィが関わっていてもイベント段階では除外しない

【回答形式】JSONの配列のみ出力（他の文章は不要）:
[
  {{
    "name": "イベントの正式名称（または推定名称）",
    "url": "イベントのURL",
    "venue": "主な開催会場（例: 東京ビッグサイト、幕張メッセ、TKP渋谷、自社セミナールーム、オンライン）",
    "timing": "開催時期（例: 毎年10月、2025年3月、不明）",
    "format": "形式（例: 屋内、屋外、オンライン、ハイブリッド）",
    "snippet": "概要（50文字以内）"
  }}
]
"""

    try:
        genai.configure(api_key=gemini_api_key)
        model = genai.GenerativeModel(GEMINI_MODEL)
        response = model.generate_content(prompt)
        parsed = _extract_json(response.text.strip())

        if isinstance(parsed, list) and parsed:
            # 必須キーが揃っているものだけ返す
            validated = []
            for item in parsed:
                if isinstance(item, dict) and item.get("name") and item.get("url"):
                    validated.append({
                        "name": item.get("name", "不明"),
                        "url": item.get("url", ""),
                        "venue": item.get("venue", "不明"),
                        "timing": item.get("timing", "不明"),
                        "format": item.get("format", "不明"),
                        "snippet": item.get("snippet", ""),
                    })
            return validated

    except Exception as exc:
        st.warning(f"Gemini検証エラー: {exc}")

    # Gemini失敗時のフォールバック: 候補をそのまま返す
    return [
        {
            "name": c["title"],
            "url": c["url"],
            "venue": "不明",
            "timing": "不明",
            "format": "不明",
            "snippet": c.get("snippet", ""),
        }
        for c in candidates[:15]
    ]


# ============================================================
# STEP 2: 企業リスト収集
# ============================================================

def _fetch_page_companies(event_url: str) -> list[dict]:
    """
    イベントページを直接スクレイピングし、
    主催・出展・スポンサーセクションのリンクから企業情報を収集する。
    """
    companies: list[dict] = []
    section_keywords = [
        "主催", "運営", "出展", "スポンサー", "協賛", "後援",
        "exhibitor", "sponsor", "organizer", "partner",
    ]

    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        }
        resp = requests.get(event_url, headers=headers, timeout=12)
        resp.encoding = resp.apparent_encoding
        soup = BeautifulSoup(resp.text, "lxml")
        event_domain = urlparse(event_url).netloc

        for block in soup.find_all(["section", "div", "article", "li"]):
            block_text = block.get_text(separator=" ")
            if any(kw in block_text for kw in section_keywords):
                for a_tag in block.find_all("a", href=True):
                    full_url = urljoin(event_url, a_tag["href"])
                    parsed = urlparse(full_url)
                    name = a_tag.get_text(strip=True)
                    if (
                        parsed.scheme in ("http", "https")
                        and parsed.netloc
                        and parsed.netloc != event_domain
                        and name and len(name) >= 2
                    ):
                        companies.append({
                            "name": name,
                            "url": full_url,
                            "domain": parsed.netloc,
                            "source": "ページ解析",
                        })
    except Exception:
        pass

    return companies


def _scrape_company_page(url: str, max_chars: int = 2500) -> str:
    """
    企業ホームページの本文テキストを取得し、Gemini判定の参考情報として渡す。
    取得できない場合は空文字を返す。
    """
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        }
        resp = requests.get(url, headers=headers, timeout=8)
        resp.encoding = resp.apparent_encoding
        soup = BeautifulSoup(resp.text, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "head", "noscript"]):
            tag.decompose()
        text = soup.get_text(separator=" ", strip=True)
        text = re.sub(r"\s{2,}", " ", text)
        return text[:max_chars]
    except Exception:
        return ""


def _search_related_companies(
    event_title: str,
    serpapi_key: str,
    extra_keyword: str = "",
) -> list[dict]:
    """
    SerpAPI（Google検索）で関連企業を収集する。
    展示会の出展企業だけでなく、セミナー・TKP開催企業も対象。
    """
    companies: list[dict] = []

    queries = [
        f'"{event_title}" 出展企業 OR 主催者 OR 運営会社',
        f'"{event_title}" セミナー OR 講演会 OR 自社開催 東京',
        f'"{event_title}" TKP OR 貸し会議室 OR スポンサー OR 協賛',
    ]
    if extra_keyword:
        queries.append(f'"{event_title}" {extra_keyword}')

    for query in queries[:4]:
        results = _serpapi_search(query, serpapi_key, MAX_SEARCH_RESULTS_PER_QUERY)
        for item in results:
            url = item.get("link", "")
            parsed = urlparse(url)
            companies.append({
                "name": item.get("title", ""),
                "url": url,
                "domain": parsed.netloc,
                "source": f"SerpAPI検索: {query[:25]}…",
            })
        time.sleep(0.3)

    return companies


def collect_companies(
    event: dict,
    serpapi_key: str,
    extra_keyword: str = "",
) -> list[dict]:
    """
    イベントに関連する企業を収集し、ドメイン単位で重複除去して返す。
    各企業に event_name を付与する。
    """
    event_name = event.get("name", event.get("title", "不明"))
    raw: list[dict] = []

    raw.extend(_fetch_page_companies(event.get("url", "")))
    raw.extend(_search_related_companies(event_name, serpapi_key, extra_keyword))

    seen_domains: set[str] = set()
    deduped: list[dict] = []
    for c in raw:
        domain = c.get("domain", "")
        if domain and domain not in seen_domains:
            seen_domains.add(domain)
            deduped.append({**c, "event_name": event_name})

    return deduped


# ============================================================
# 除外フィルタ
# ============================================================

def _is_excluded(company: dict, extra_keywords: "list[str] | None" = None) -> bool:
    """
    エル・アミティエ・フェアリィおよびユーザー追加キーワードに
    該当する企業を除外判定する。
    """
    name_lower = company.get("name", "").lower()
    url_lower = company.get("url", "").lower()
    domain_lower = company.get("domain", "").lower()

    for domain in EXCLUSION_DOMAINS:
        if domain in domain_lower or domain in url_lower:
            return True
    for kw in EXCLUSION_KEYWORDS:
        if kw.lower() in name_lower or kw.lower() in url_lower:
            return True
    if extra_keywords:
        for kw in extra_keywords:
            kl = kw.lower()
            if kl and (kl in name_lower or kl in url_lower):
                return True
    return False


def apply_exclusion_filter(
    companies: list[dict],
    extra_keywords: "list[str] | None" = None,
) -> "tuple[list[dict], list[dict]]":
    valid, excluded = [], []
    for c in companies:
        (excluded if _is_excluded(c, extra_keywords) else valid).append(c)
    return valid, excluded


# ============================================================
# STEP 3: Gemini APIによる企業AI判定（12項目一括抽出）
# ============================================================

def _build_company_prompt(
    company: dict,
    page_text: str,
    genre_label: str,
) -> str:
    page_section = (
        f"\n【企業サイトから取得したテキスト（参考）】\n{page_text[:2000]}"
        if page_text else "\n【企業サイトの情報は取得できませんでした】"
    )

    return f"""あなたは日本の営業リサーチ専門家です。
以下の企業について調査し、指定のJSON形式のみで回答してください（JSON以外の文章は一切不要）。

【調査対象】
企業名: {company.get("name", "不明")}
URL: {company.get("url", "不明")}
関連イベント: {company.get("event_name", "不明")}
関連ジャンル: {genre_label}
{page_section}

【判定①: 東京エリア該当（tokyo_area）】
対象: {TOKYO_AREA_HINT}
本社・主要拠点が上記エリア内 → true、不明・地方・海外 → false

【判定②: MC・ナレーター需要あり（mc_related）】
以下のいずれかに当てはまる場合 true:
- 展示会・見本市・博覧会の主催・共催
- 自社セミナー・講演会・カンファレンスを定期開催
- TKP等の貸し会議室でイベント開催
- YouTube・ウェビナーでMC・司会・ナレーター入り配信
- イベント企画・制作・運営業（自らMCを手配する立場）
※ MC人材を「売る側」（タレント事務所・キャスティング会社）は false

【判定③: 除外リスク（exclusion_risk）】
「エル・アミティエ（l-amitie）」「フェアリィ（fairy）」が常駐・深く関与する
広告代理店・イベント制作会社と判断できる場合 true

【抽出項目】（不明・取得不可の場合は必ず "不明" と記入）
- contact: 代表メール or 問い合わせフォームURL（1件）
- contact_title: 担当者の肩書
- contact_name: 担当者の名前
- event_timing: 主なイベント開催時期（例: 毎年3月・10月、不定期）
- event_venue: 主な開催会場（例: 東京ビッグサイト、TKP渋谷、自社セミナールーム、YouTube）
- event_format: イベント形式（例: 屋内、オンライン、ハイブリッド）
- event_details: ジャンルや特徴・詳細（100文字以内）
- mc_job: ステージの種類・MC業務内容（例: トークショー司会、プレゼン前振り後振り、掛け合い、暗記あり/なし）
- mc_scene: MCの想定シーン（例: メインステージ、オープニングセレモニー、ウェビナー冒頭）

【必須出力JSONフォーマット】
{{
  "tokyo_area": true,
  "mc_related": true,
  "exclusion_risk": false,
  "exclusion_reason": "",
  "contact": "不明",
  "contact_title": "不明",
  "contact_name": "不明",
  "event_timing": "不明",
  "event_venue": "不明",
  "event_format": "不明",
  "event_details": "不明",
  "mc_job": "不明",
  "mc_scene": "不明"
}}
"""


def classify_company(
    company: dict,
    model: genai.GenerativeModel,
    genre_label: str,
) -> dict:
    """
    Gemini API で1社ずつ判定し、13項目すべてを含む辞書を返す。
    取得できなかった項目は "不明" で埋める。
    """
    page_text = _scrape_company_page(company.get("url", ""))

    fallback: dict = {
        "tokyo_area": False, "mc_related": False,
        "exclusion_risk": False, "exclusion_reason": "",
        "contact": "不明", "contact_title": "不明", "contact_name": "不明",
        "event_timing": "不明", "event_venue": "不明", "event_format": "不明",
        "event_details": "不明", "mc_job": "不明", "mc_scene": "不明",
    }

    try:
        prompt = _build_company_prompt(company, page_text, genre_label)
        response = model.generate_content(prompt)
        parsed = _extract_json(response.text.strip())

        if not isinstance(parsed, dict):
            fallback["event_details"] = "JSONパース失敗"
            return {**company, **fallback}

        merged = {**fallback, **{k: v for k, v in parsed.items() if v is not None}}
        return {**company, **merged}

    except Exception as exc:
        fallback["event_details"] = f"APIエラー: {exc}"
        return {**company, **fallback}


def run_ai_classification(
    companies: list[dict],
    gemini_api_key: str,
    delay_seconds: int,
    genre_label: str,
    progress_bar,
    status_text,
) -> list[dict]:
    """
    企業リスト全体を順番にGemini判定する。
    各社のサイトをスクレイピング後に判定するためウェイトを必ず挟む。
    """
    genai.configure(api_key=gemini_api_key)
    model = genai.GenerativeModel(GEMINI_MODEL)
    results: list[dict] = []
    total = len(companies)

    for i, company in enumerate(companies):
        status_text.markdown(
            f"**判定中 ({i + 1} / {total}):** {company.get('name', '不明')}　"
            f"（サイト取得 → Gemini判定 → {delay_seconds}秒待機）"
        )
        result = classify_company(company, model, genre_label)
        results.append(result)
        progress_bar.progress((i + 1) / total)

        if i < total - 1:
            time.sleep(delay_seconds)

    return results


# ============================================================
# STEP 4: 出力
# ============================================================

def _build_csv_dataframe(companies: list[dict]) -> pd.DataFrame:
    """
    OUTPUT_COLUMNS の順番通りの DataFrame を生成する。
    欠損値はすべて「不明」で埋める。
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    rows = []
    for c in companies:
        rows.append({
            "イベントジャンル（50選）":       c.get("genre_label", "不明"),
            "AIが自動リサーチしたイベント名":  c.get("event_name", "不明"),
            "企業名":                         c.get("name", "不明"),
            "企業のHP URL":                   c.get("url", "不明"),
            "連絡先（代表メールや問い合わせ窓口）": c.get("contact", "不明"),
            "担当者肩書":                     c.get("contact_title", "不明"),
            "担当者名":                       c.get("contact_name", "不明"),
            "イベント開催時期":               c.get("event_timing", "不明"),
            "開催会場":                       c.get("event_venue", "不明"),
            "イベント形式":                   c.get("event_format", "不明"),
            "ジャンルや特徴・詳細":           c.get("event_details", "不明"),
            "ステージの種類・MC業務内容":      c.get("mc_job", "不明"),
            "MCの想定シーン":                 c.get("mc_scene", "不明"),
            "情報ソース":                     c.get("source", ""),
            "書き込み日時":                   now_str,
        })
    df = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    return df.fillna("不明")


def export_to_google_sheets(
    companies: list[dict],
    spreadsheet_ref: str,
    credentials_json_str: str,
    sheet_name: str,
) -> "tuple[bool, str]":
    """
    サービスアカウント認証でGoogleスプレッドシートに企業リストを追記する。
    """
    try:
        creds_data = json.loads(credentials_json_str)
    except json.JSONDecodeError:
        return False, "サービスアカウントJSONの形式が正しくありません"

    try:
        creds = Credentials.from_service_account_info(creds_data, scopes=GS_SCOPES)
        client = gspread.authorize(creds)

        match = re.search(r"/d/([a-zA-Z0-9_-]+)", spreadsheet_ref)
        spreadsheet_id = match.group(1) if match else spreadsheet_ref.strip()
        spreadsheet = client.open_by_key(spreadsheet_id)

        try:
            sheet = spreadsheet.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            sheet = spreadsheet.add_worksheet(title=sheet_name, rows=2000, cols=20)

        if not sheet.get_all_values():
            sheet.append_row(OUTPUT_COLUMNS, value_input_option="USER_ENTERED")

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        rows = [
            [
                c.get("genre_label", "不明"),
                c.get("event_name", "不明"),
                c.get("name", ""),
                c.get("url", ""),
                c.get("contact", "不明"),
                c.get("contact_title", "不明"),
                c.get("contact_name", "不明"),
                c.get("event_timing", "不明"),
                c.get("event_venue", "不明"),
                c.get("event_format", "不明"),
                c.get("event_details", "不明"),
                c.get("mc_job", "不明"),
                c.get("mc_scene", "不明"),
                c.get("source", ""),
                now_str,
            ]
            for c in companies
        ]
        if rows:
            sheet.append_rows(rows, value_input_option="USER_ENTERED")

        return True, f"{len(rows)}件をシート「{sheet_name}」に追記しました"

    except Exception as exc:
        return False, f"スプレッドシートエラー: {exc}"


# ============================================================
# サイドバー
# ============================================================

def render_sidebar() -> dict:
    with st.sidebar:
        st.title("⚙️ 設定")

        st.subheader("🔑 SerpAPI（Google検索）")
        serpapi_key = st.text_input(
            "SerpAPI Key",
            value=os.getenv("SERPAPI_KEY", ""),
            type="password",
            help="serpapi.com で無料登録して取得（無料枠: 月100回）",
        )

        st.subheader("🤖 Gemini API")
        gemini_api_key = st.text_input(
            "Gemini API Key",
            value=os.getenv("GEMINI_API_KEY", ""),
            type="password",
            help="Google AI Studio（aistudio.google.com）で無料取得",
        )

        st.subheader("📊 Googleスプレッドシート")
        gs_credentials = st.text_area(
            "サービスアカウント JSON",
            value=os.getenv("GS_CREDENTIALS_JSON", ""),
            height=90,
            help="JSONキーファイルの内容をそのまま貼り付け",
            placeholder='{"type":"service_account","project_id":"..."}',
        )
        gs_spreadsheet = st.text_input(
            "スプレッドシート URL または ID",
            value=os.getenv("GS_SPREADSHEET_ID", ""),
        )
        gs_sheet_name = st.text_input("シート名", value="営業リスト")

        st.subheader("⏱️ レートリミット設定")
        delay_seconds = st.slider(
            "Gemini API 呼び出し間隔（秒）",
            min_value=2, max_value=20, value=5,
            help="無料枠: 1分あたり15リクエスト。エラーが出たら増やしてください",
        )

        st.subheader("🔍 イベント検索設定")
        max_queries = st.slider(
            "ジャンルキーワードの使用数",
            min_value=1, max_value=5, value=3,
            help="多いほど多くのイベントを発見できますが、Google APIの消費が増えます",
        )

        st.subheader("🚫 除外フィルタ")
        st.caption("デフォルト除外（変更不可）")
        st.markdown("- エル・アミティエ（l-amitie.co.jp）")
        st.markdown("- フェアリィ（fairy1990.com）")
        additional_exclusions_raw = st.text_area(
            "追加除外キーワード（改行区切り）",
            placeholder="例:\n〇〇広告代理店\nexample-agency.co.jp",
        )
        additional_exclusions = [
            line.strip()
            for line in additional_exclusions_raw.splitlines()
            if line.strip()
        ]

    return {
        "serpapi_key": serpapi_key,
        "gemini_api_key": gemini_api_key,
        "gs_credentials": gs_credentials,
        "gs_spreadsheet": gs_spreadsheet,
        "gs_sheet_name": gs_sheet_name,
        "delay_seconds": delay_seconds,
        "max_queries": max_queries,
        "additional_exclusions": additional_exclusions,
    }


# ============================================================
# STEP1 タブ: 完全自動イベントリサーチ
# ============================================================

def render_step1(cfg: dict) -> None:
    st.header("🔍 STEP 1 ― ジャンル選択 → 完全自動イベントリサーチ")
    st.info(
        "ジャンルを選択してボタンを押すと、GoogleとGeminiが連動して\n"
        "東京都内・近郊で開催されるイベントを**全自動でリサーチ**します。"
    )

    # ① ジャンル選択
    selected_label = st.selectbox(
        "📂 イベントジャンル（50カテゴリ）",
        options=GENRE_LABELS,
        index=0,
        help="選択すると対応するキーワードで自動検索します",
    )

    genre: dict | None = None
    if selected_label != "― ジャンルを選択してください ―":
        genre = next((g for g in GENRE_LIST if g["label"] == selected_label), None)

    # 選択ジャンルのキーワード一覧を表示
    if genre:
        with st.expander(f"💡 「{selected_label}」の検索キーワード候補"):
            st.caption(f"上位 {cfg['max_queries']} 件のキーワードを使用します（サイドバーで変更可）")
            for i, kw in enumerate(genre["keywords"]):
                mark = "✅" if i < cfg["max_queries"] else "⬜"
                st.markdown(f"{mark} `{kw}`")

    # ② 自動リサーチ実行ボタン
    st.divider()
    col_btn, col_info = st.columns([2, 3])
    with col_btn:
        search_clicked = st.button(
            "🚀 自動リサーチ開始",
            type="primary",
            use_container_width=True,
            disabled=(genre is None),
        )
    with col_info:
        if genre is None:
            st.warning("⬅️ まずジャンルを選択してください")
        else:
            st.caption(
                f"**処理内容**:\n"
                f"1. Google検索（{cfg['max_queries']}クエリ）でイベント候補を収集\n"
                f"2. Gemini APIでイベントを整理・絞り込み（1回のみ）"
            )

    if search_clicked:
        if not cfg["serpapi_key"]:
            st.error("⚠️ SerpAPI Key を設定してください")
            return
        if not cfg["gemini_api_key"]:
            st.error("⚠️ Gemini API Key を設定してください（イベント整理に必要です）")
            return

        # ステップA: SerpAPI（Google検索）
        with st.spinner(f"🔍 Googleで「{selected_label}」関連イベントを検索中…"):
            candidates = auto_research_events(
                genre, cfg["serpapi_key"], cfg["max_queries"]
            )

        if not candidates:
            st.warning("Google検索でイベント候補が見つかりませんでした。キーワードやAPI設定を確認してください。")
            return

        st.caption(f"Google検索で {len(candidates)} 件の候補を取得しました → Geminiで整理中…")

        # ステップB: Gemini整理
        with st.spinner("🤖 GeminiがイベントリストをAIで整理中（1〜2分かかる場合があります）…"):
            events = validate_events_with_gemini(
                candidates, selected_label, cfg["gemini_api_key"]
            )

        st.session_state.events = events
        st.session_state.selected_genre_label = selected_label
        # 選択変更時に後続をリセット
        st.session_state.selected_event = None
        st.session_state.companies = []
        st.session_state.excluded_companies = []
        st.session_state.ai_results = []
        st.session_state.filtered_companies = []

        if events:
            st.success(f"✅ {len(events)} 件のイベントを抽出しました！")
        else:
            st.warning("イベントが見つかりませんでした。ジャンルや検索設定を変更してみてください。")

    # ③ イベント一覧の表示・選択
    if st.session_state.events:
        st.subheader(f"📋 自動リサーチ結果（{len(st.session_state.events)} 件）")
        st.caption("👇 営業対象にしたいイベントを選択して STEP2 に進んでください")

        for i, event in enumerate(st.session_state.events):
            with st.expander(
                f"📌 {event.get('name', '不明')}",
                expanded=(i == 0),
            ):
                col1, col2 = st.columns([3, 1])
                with col1:
                    st.markdown(f"**URL:** [{event.get('url', '')}]({event.get('url', '')})")
                    st.markdown(f"**会場:** {event.get('venue', '不明')}　"
                                f"**時期:** {event.get('timing', '不明')}　"
                                f"**形式:** {event.get('format', '不明')}")
                    if event.get("snippet"):
                        st.caption(event["snippet"])
                with col2:
                    if st.button(
                        "このイベントを選択\n→ STEP2へ",
                        key=f"select_event_{i}",
                        type="secondary",
                        use_container_width=True,
                    ):
                        st.session_state.selected_event = event
                        st.session_state.companies = []
                        st.session_state.excluded_companies = []
                        st.session_state.ai_results = []
                        st.session_state.filtered_companies = []
                        st.success(
                            f"「{event.get('name', '不明')}」を選択しました。"
                            f"STEP2 タブに進んでください。"
                        )


# ============================================================
# STEP2 タブ: 企業リスト収集
# ============================================================

def render_step2(cfg: dict) -> None:
    st.header("🏢 STEP 2 ― 企業リスト収集")

    if not st.session_state.selected_event:
        st.info("👆 STEP1 でイベントを選択してください")
        return

    event = st.session_state.selected_event
    genre_label = st.session_state.get("selected_genre_label", "不明")

    st.success(f"**選択中のイベント:** {event.get('name', '不明')}")
    st.markdown(f"URL: [{event.get('url', '')}]({event.get('url', '')})")
    st.caption(
        f"会場: {event.get('venue', '不明')}　"
        f"時期: {event.get('timing', '不明')}　"
        f"形式: {event.get('format', '不明')}"
    )

    extra_keyword = st.text_input(
        "追加検索ワード（任意）",
        placeholder="例: 出展企業　広告代理店　主催者",
        help="このワードを加えてより詳細な企業情報を収集します",
    )

    if st.button("🏢 企業リストを収集する", type="primary", use_container_width=True):
        if not cfg["serpapi_key"]:
            st.error("⚠️ SerpAPI Key を設定してください")
            return

        with st.spinner("企業情報を収集中…（30秒〜1分程度かかる場合があります）"):
            raw_companies = collect_companies(
                event, cfg["serpapi_key"], extra_keyword
            )

        valid, excluded = apply_exclusion_filter(raw_companies, cfg["additional_exclusions"])
        st.session_state.companies = valid
        st.session_state.excluded_companies = excluded
        st.session_state.ai_results = []
        st.session_state.filtered_companies = []

        st.success(f"✅ 収集完了: **{len(valid)} 社**（除外: {len(excluded)} 社）")

    if st.session_state.excluded_companies:
        with st.expander(f"🚫 除外された企業 ({len(st.session_state.excluded_companies)} 社)"):
            ex_df = pd.DataFrame(st.session_state.excluded_companies)
            show = [c for c in ["name", "url", "source"] if c in ex_df.columns]
            st.dataframe(
                ex_df[show].rename(columns={"name": "企業名", "url": "URL", "source": "情報ソース"}),
                use_container_width=True, hide_index=True,
            )

    if st.session_state.companies:
        st.subheader(f"📋 収集企業リスト（{len(st.session_state.companies)} 社）")
        df = pd.DataFrame(st.session_state.companies)
        show = [c for c in ["name", "url", "event_name", "source"] if c in df.columns]
        st.dataframe(
            df[show].rename(columns={
                "name": "企業名", "url": "URL",
                "event_name": "関連イベント", "source": "情報ソース",
            }),
            use_container_width=True, hide_index=True,
        )
        st.info("➡️ STEP3 タブで AI による絞り込みを実行してください")


# ============================================================
# STEP3 タブ: AI絞り込み
# ============================================================

def render_step3(cfg: dict) -> None:
    st.header("🤖 STEP 3 ― AI絞り込み（Gemini API）")

    if not st.session_state.companies:
        st.info("👆 STEP2 で企業リストを収集してください")
        return

    total = len(st.session_state.companies)
    estimated_sec = total * (cfg["delay_seconds"] + 3)
    genre_label = st.session_state.get("selected_genre_label", "不明")

    st.info(
        f"**{total} 社**を対象に以下を一括判定します。\n\n"
        f"① 東京都内・近郊エリアの企業かどうか\n"
        f"② MC・ナレーターを必要とするイベント開催実績があるか\n"
        f"③ エル・アミティエ / フェアリィと深い関係がある企業でないか\n"
        f"④ 13項目の営業情報を同時抽出\n\n"
        f"⏱️ 予想所要時間: 約 **{estimated_sec // 60} 分 {estimated_sec % 60} 秒**"
        f"（{cfg['delay_seconds']} 秒間隔）"
    )

    if st.button("🤖 AI判定を開始する", type="primary", use_container_width=True):
        if not cfg["gemini_api_key"]:
            st.error("⚠️ Gemini API Key を設定してください")
            return

        progress_bar = st.progress(0)
        status_text = st.empty()

        ai_results = run_ai_classification(
            st.session_state.companies,
            cfg["gemini_api_key"],
            cfg["delay_seconds"],
            genre_label,
            progress_bar,
            status_text,
        )

        # フィルタ条件:
        # ① tokyo_area=True  ② mc_related=True  ③ exclusion_risk=False
        # ④ ハードコード除外リスト非該当
        filtered = [
            {**c, "genre_label": genre_label}
            for c in ai_results
            if c.get("tokyo_area") is True
            and c.get("mc_related") is True
            and not c.get("exclusion_risk", False)
            and not _is_excluded(c, cfg["additional_exclusions"])
        ]

        st.session_state.ai_results = ai_results
        st.session_state.filtered_companies = filtered

        status_text.markdown("✅ **判定完了！**")
        progress_bar.progress(1.0)

        tokyo_ok = sum(1 for c in ai_results if c.get("tokyo_area"))
        mc_ok = sum(1 for c in ai_results if c.get("mc_related"))
        excl = sum(1 for c in ai_results if c.get("exclusion_risk"))
        st.success(
            f"✅ {total} 社を判定しました。\n\n"
            f"- 東京エリア該当: {tokyo_ok} 社\n"
            f"- MC・ナレーター需要あり: {mc_ok} 社\n"
            f"- 除外リスクあり（除外済み）: {excl} 社\n"
            f"- **最終残存: {len(filtered)} 社**"
        )

    # ---- AI判定全件 ----
    if st.session_state.ai_results:
        st.subheader("📊 AI判定結果（全件）")

        all_df = pd.DataFrame(st.session_state.ai_results)

        def _status(row: pd.Series) -> str:
            if row.get("exclusion_risk"):
                return "🚫 除外リスク"
            if not row.get("tokyo_area"):
                return "🗾 エリア外"
            if not row.get("mc_related"):
                return "❌ MC需要なし"
            return "✅ 通過"

        all_df["判定ステータス"] = all_df.apply(_status, axis=1)
        col_map = {
            "name": "企業名", "url": "URL",
            "event_name": "関連イベント",
            "判定ステータス": "判定ステータス",
            "event_venue": "開催会場", "event_format": "形式",
            "mc_job": "MC業務内容",
        }
        show = [c for c in col_map if c in all_df.columns or c == "判定ステータス"]
        st.dataframe(
            all_df[show].rename(columns=col_map),
            use_container_width=True, hide_index=True,
        )

    # ---- 最終結果（13項目） ----
    if st.session_state.filtered_companies:
        st.subheader(f"✅ 最終絞り込み結果（{len(st.session_state.filtered_companies)} 社）")
        final_df = pd.DataFrame(st.session_state.filtered_companies)
        col_map2 = {
            "genre_label": "ジャンル",
            "event_name": "イベント名",
            "name": "企業名", "url": "企業URL",
            "contact": "連絡先",
            "contact_title": "担当者肩書",
            "contact_name": "担当者名",
            "event_timing": "開催時期",
            "event_venue": "開催会場",
            "event_format": "形式",
            "event_details": "詳細",
            "mc_job": "MC業務内容",
            "mc_scene": "MCシーン",
        }
        show2 = [c for c in col_map2 if c in final_df.columns]
        st.dataframe(
            final_df[show2].rename(columns=col_map2),
            use_container_width=True, hide_index=True,
        )
        st.info("➡️ STEP4 タブで CSV をダウンロード、またはスプレッドシートに出力してください")


# ============================================================
# STEP4 タブ: 出力
# ============================================================

def render_step4(cfg: dict) -> None:
    st.header("📤 STEP 4 ― 出力（CSV ダウンロード / スプレッドシート書き込み）")

    if not st.session_state.filtered_companies:
        st.info("👆 STEP3 でAI絞り込みを完了させてください")
        return

    companies = st.session_state.filtered_companies
    st.success(f"**{len(companies)} 社**の営業リストが完成しました")

    with st.expander("📋 出力データのプレビュー（全15列）"):
        st.dataframe(_build_csv_dataframe(companies), use_container_width=True, hide_index=True)

    st.divider()

    # ---- CSVダウンロード ----
    st.subheader("💾 CSVダウンロード（推奨）")
    st.markdown(
        "**Googleスプレッドシートへの取り込み手順（Mac）:**\n"
        "1. 下のボタンでCSVを保存\n"
        "2. [Googleドライブ](https://drive.google.com) を開く\n"
        "3. CSVをドラッグ＆ドロップ\n"
        "4. ファイルを右クリック → **「アプリで開く」→「Googleスプレッドシート」**\n"
        "5. 日本語が文字化けなく表示されます ✅"
    )

    csv_df = _build_csv_dataframe(companies)
    csv_bytes = csv_df.to_csv(index=False, encoding="utf-8-sig").encode("utf-8-sig")
    filename = f"営業リスト_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"

    st.download_button(
        label="📥 CSVをダウンロード（BOM付きUTF-8 ／ 文字化けなし）",
        data=csv_bytes,
        file_name=filename,
        mime="text/csv; charset=utf-8-sig",
        use_container_width=True,
        type="primary",
    )

    st.divider()

    # ---- スプレッドシート直接書き込み ----
    st.subheader("📊 Googleスプレッドシートへ直接書き込み（任意）")
    st.caption("サービスアカウントJSONを設定している場合のみ利用可能")

    if st.button("📤 スプレッドシートに書き込む", use_container_width=True):
        if not cfg["gs_credentials"]:
            st.error("⚠️ サービスアカウント JSON を設定してください")
        elif not cfg["gs_spreadsheet"]:
            st.error("⚠️ スプレッドシートの URL または ID を設定してください")
        else:
            with st.spinner("書き込み中…"):
                success, message = export_to_google_sheets(
                    companies,
                    cfg["gs_spreadsheet"],
                    cfg["gs_credentials"],
                    cfg["gs_sheet_name"],
                )
            if success:
                st.success(f"✅ {message}")
                url_match = re.search(
                    r"https://docs\.google\.com/spreadsheets/d/[^/]+",
                    cfg["gs_spreadsheet"],
                )
                if url_match:
                    st.markdown(f"[📊 スプレッドシートを開く]({url_match.group(0)})")
            else:
                st.error(f"❌ {message}")


# ============================================================
# メインエントリーポイント
# ============================================================

def main() -> None:
    st.set_page_config(
        page_title="イベント関連企業リサーチツール",
        page_icon="🎯",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    init_session_state()

    st.title("🎯 イベント関連企業 完全自動リサーチ・絞り込みツール")
    st.caption(
        "ジャンル選択 → Google ＋ Gemini で全自動イベント検索 → 企業収集 → AI絞り込み → CSV出力\n"
        "エル・アミティエ・フェアリィ関連は企業段階で自動除外"
    )

    cfg = render_sidebar()

    tab1, tab2, tab3, tab4 = st.tabs([
        "🔍 STEP1: 自動イベント検索",
        "🏢 STEP2: 企業収集",
        "🤖 STEP3: AI絞り込み",
        "📤 STEP4: CSV・スプレッドシート出力",
    ])

    with tab1:
        render_step1(cfg)
    with tab2:
        render_step2(cfg)
    with tab3:
        render_step3(cfg)
    with tab4:
        render_step4(cfg)


if __name__ == "__main__":
    main()
