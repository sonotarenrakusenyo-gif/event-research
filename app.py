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
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import pandas as pd
import time
import json
import re
import io
from urllib.parse import urljoin, urlparse
from datetime import datetime
import os
import tempfile
import zipfile
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

# 利用したいGeminiモデルの優先順位（上から順に、使えるものを自動採用）
# flash-lite は無料枠のレート制限が緩い（1分あたりの回数が多い）ため優先
GEMINI_MODEL_PREFERENCES = [
    "gemini-2.5-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-2.5-flash",
    "gemini-flash-latest",
    "gemini-2.0-flash",
]

# 解決済みモデル名・モデルチェーンのキャッシュ（毎回 list_models を呼ばないため）
_RESOLVED_GEMINI_MODEL: "str | None" = None
_RESOLVED_GEMINI_CHAIN: "list[str] | None" = None

GS_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

BACKUP_SHEET_TAB = "ツールバックアップ"
SHEET_BACKUP_CHUNK_SIZE = 45000

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

MAX_SEARCH_RESULTS_PER_QUERY = 20

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
# セッションステートの初期化 ＆ 結果の永続化（リロードしても残す）
# ============================================================

# リロードしても保持したいキー（取得結果はAPI枠を使うので消したくない）
PERSIST_KEYS = [
    "events",
    "selected_event",
    "selected_genre_label",
    "companies",
    "excluded_companies",
    "ai_results",
    "filtered_companies",
    "collect_stats",
    "sales_emails",
]

# 保存先（サーバー上の一時領域。同一サーバー稼働中のリロードでは残るが、
# Streamlit Cloudのスリープ・再起動・再デプロイでは消える）
STATE_FILE = os.path.join(tempfile.gettempdir(), "oshigoto_tool_state.json")


def save_state() -> None:
    """保持対象のセッション内容をファイルに保存する（API消費なし）。"""
    try:
        data = {k: st.session_state.get(k) for k in PERSIST_KEYS}
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def _load_saved_state() -> None:
    """保存済みの結果があればセッションに復元する（API消費なし）。"""
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        for key, val in data.items():
            if val is not None:
                st.session_state[key] = val
    except Exception:
        pass


def clear_saved_state() -> None:
    """保存済みの結果を消去し、セッションも初期化する。"""
    try:
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
    except Exception:
        pass
    for key in PERSIST_KEYS:
        if key in st.session_state:
            del st.session_state[key]


def _genre_to_sheet_tab(genre_label: str) -> str:
    """ジャンル名からスプレッドシートのタブ名を生成する（禁止文字を除去）。"""
    name = re.sub(r"^\d+\.\s*", "", (genre_label or "営業リスト").strip())
    name = re.sub(r"[\[\]\\/?*]", "", name).strip()
    return (name[:100] if name else "営業リスト")


def _custom_genre_from_input(text: str) -> dict:
    """自由入力テキストから検索用ジャンル情報を生成する。"""
    word = text.strip()
    return {
        "label": word,
        "keywords": [
            f"{word} 展示会",
            f"{word} イベント",
            f"{word} カンファレンス",
            f"{word} セミナー",
            f"{word} EXPO",
        ],
    }


def _resolve_search_genre(
    selected_label: str,
    custom_input: str,
) -> "tuple[dict | None, str]":
    """
    プルダウンまたは自由入力から検索に使うジャンルを決定する。
    自由入力がある場合はそちらを優先する。
    """
    custom = (custom_input or "").strip()
    if custom:
        return _custom_genre_from_input(custom), custom

    if selected_label != "― ジャンルを選択してください ―":
        genre = next((g for g in GENRE_LIST if g["label"] == selected_label), None)
        if genre:
            return genre, selected_label
    return None, ""


def _get_secret(key: str, default: str = "") -> str:
    """Streamlit Secrets → 環境変数 → デフォルトの順で設定値を取得する。"""
    try:
        if key in st.secrets:
            val = st.secrets[key]
            if val is not None and str(val).strip():
                return str(val).strip()
    except Exception:
        pass
    return os.getenv(key, default).strip()


def _normalize_drive_folder_id(folder_ref: str) -> str:
    """DriveフォルダURLまたはIDからフォルダID部分だけを取り出す。"""
    ref = (folder_ref or "").strip()
    match = re.search(r"/folders/([a-zA-Z0-9_-]+)", ref)
    return match.group(1) if match else ref


def _effective_gs_credentials(widget_value: str) -> str:
    """
    サービスアカウントJSONを取得する。
    Streamlit CloudではSecretsの値を優先（サイドバーの短文表示欄は長いJSONが欠けることがある）。
    """
    secret_val = _get_secret("GS_CREDENTIALS_JSON")
    if secret_val:
        return secret_val
    return (widget_value or "").strip()


def _gs_is_configured(cfg: dict) -> bool:
    """Googleスプレッドシート連携に必要な設定が揃っているか。"""
    return bool(cfg.get("gs_credentials") and cfg.get("gs_spreadsheet"))


def _build_backup_payload() -> str:
    """Drive/Sheetsバックアップ用のJSON文字列を生成する。"""
    return json.dumps(
        {k: st.session_state.get(k) for k in PERSIST_KEYS},
        ensure_ascii=False,
        sort_keys=True,
    )


def backup_state_to_sheet(
    credentials_json_str: str,
    spreadsheet_ref: str,
    sheet_name: str = BACKUP_SHEET_TAB,
) -> "tuple[bool, str]":
    """
    作業状態JSONをスプレッドシートの専用タブに保存する。
    個人Googleアカウントでもサービスアカウント経由で保存できる。
    """
    try:
        creds_data = json.loads(credentials_json_str)
    except json.JSONDecodeError:
        return False, "サービスアカウントJSONの形式が正しくありません"

    try:
        payload = _build_backup_payload()
        chunks = [
            payload[i : i + SHEET_BACKUP_CHUNK_SIZE]
            for i in range(0, len(payload), SHEET_BACKUP_CHUNK_SIZE)
        ] or [""]

        creds = Credentials.from_service_account_info(creds_data, scopes=GS_SCOPES)
        client = gspread.authorize(creds)

        match = re.search(r"/d/([a-zA-Z0-9_-]+)", spreadsheet_ref)
        spreadsheet_id = match.group(1) if match else spreadsheet_ref.strip()
        spreadsheet = client.open_by_key(spreadsheet_id)

        try:
            sheet = spreadsheet.worksheet(sheet_name)
        except gspread.WorksheetNotFound:
            sheet = spreadsheet.add_worksheet(
                title=sheet_name,
                rows=max(100, len(chunks) + 5),
                cols=2,
            )

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        rows = [
            ["updated_at", now_str],
            ["chunk_count", str(len(chunks))],
        ]
        rows.extend([f"chunk_{i}", chunk] for i, chunk in enumerate(chunks))

        sheet.clear()
        sheet.update(rows, value_input_option="RAW")

        return True, f"スプレッドシート「{sheet_name}」タブにバックアップしました"
    except Exception as exc:
        return False, f"スプレッドシートバックアップエラー: {exc}"


def backup_state_to_drive(credentials_json_str: str, folder_id: str) -> "tuple[bool, str]":
    """
    作業状態JSONをGoogleドライブの指定フォルダに保存する。
    同名ファイル oshigoto_latest.json があれば上書き、なければ新規作成。
    """
    try:
        creds_data = json.loads(credentials_json_str)
    except json.JSONDecodeError:
        return False, "サービスアカウントJSONの形式が正しくありません"

    folder_id = _normalize_drive_folder_id(folder_id)
    if not folder_id:
        return False, "DriveフォルダIDが空です"

    try:
        creds = Credentials.from_service_account_info(creds_data, scopes=GS_SCOPES)
        service = build("drive", "v3", credentials=creds)
        payload = _build_backup_payload().encode("utf-8")
        media = MediaIoBaseUpload(
            io.BytesIO(payload), mimetype="application/json", resumable=False
        )

        query = (
            f"name='oshigoto_latest.json' and '{folder_id}' in parents "
            f"and trashed=false"
        )
        existing = (
            service.files()
            .list(q=query, fields="files(id)", supportsAllDrives=True)
            .execute()
            .get("files", [])
        )
        if existing:
            service.files().update(
                fileId=existing[0]["id"], media_body=media, supportsAllDrives=True
            ).execute()
        else:
            service.files().create(
                body={"name": "oshigoto_latest.json", "parents": [folder_id]},
                media_body=media,
                fields="id",
                supportsAllDrives=True,
            ).execute()
        return True, "Googleドライブにバックアップしました（oshigoto_latest.json）"
    except Exception as exc:
        err = str(exc)
        if "storageQuotaExceeded" in err or "storage quota" in err.lower():
            return False, (
                "個人GoogleアカウントではDrive保存できません。"
                "Workspaceの共有ドライブが必要です。"
                "代わりにスプレッドシートの「ツールバックアップ」タブをご利用ください。"
            )
        return False, f"Driveバックアップエラー: {exc}"


def _csv_cell(value, default: str = "不明") -> str:
    """CSVセルを文字列に正規化する（NaN・空欄対応）。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return default
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return default
    return text


def _csv_row_to_company(row: pd.Series) -> dict:
    """CSVの1行を filtered_companies 用の dict に変換する。"""
    return {
        "genre_label": _csv_cell(row.get("イベントジャンル（50選）", "不明")),
        "event_name": _csv_cell(row.get("AIが自動リサーチしたイベント名", "不明")),
        "name": _csv_cell(row.get("企業名", ""), ""),
        "url": _csv_cell(row.get("企業のHP URL", ""), ""),
        "contact": _csv_cell(row.get("連絡先（代表メールや問い合わせ窓口）", "不明")),
        "contact_title": _csv_cell(row.get("担当者肩書", "不明")),
        "contact_name": _csv_cell(row.get("担当者名", "不明")),
        "event_timing": _csv_cell(row.get("イベント開催時期", "不明")),
        "event_venue": _csv_cell(row.get("開催会場", "不明")),
        "event_format": _csv_cell(row.get("イベント形式", "不明")),
        "event_details": _csv_cell(row.get("ジャンルや特徴・詳細", "不明")),
        "mc_job": _csv_cell(row.get("ステージの種類・MC業務内容", "不明")),
        "mc_scene": _csv_cell(row.get("MCの想定シーン", "不明")),
        "source": _csv_cell(row.get("情報ソース", ""), ""),
    }


def import_companies_from_csv(uploaded_file) -> "tuple[bool, str]":
    """営業リストCSVから filtered_companies を復元する。"""
    try:
        df = pd.read_csv(uploaded_file, encoding="utf-8-sig")
        if df.empty or "企業名" not in df.columns:
            return False, "CSVの形式が正しくありません（企業名列が必要です）"
        companies = [_csv_row_to_company(row) for _, row in df.iterrows()]
        companies = [c for c in companies if c.get("name")]
        if not companies:
            return False, "有効な企業データがありません"
        st.session_state.filtered_companies = companies
        st.session_state.ai_results = companies
        st.session_state.sales_emails = {}  # リスト差し替え時はメール生成も最初から
        genre = companies[0].get("genre_label", "不明")
        if genre and genre != "不明":
            st.session_state.selected_genre_label = genre
        save_state()
        return True, f"{len(companies)} 社を復元しました（STEP4 / STEP5 から利用可能）"
    except Exception as exc:
        return False, f"CSV復元エラー: {exc}"


def init_session_state() -> None:
    defaults: dict = {
        "events": [],                    # 自動リサーチで得たイベントリスト
        "selected_event": None,          # 選択中のイベント dict
        "selected_genre_label": "不明",  # 選択中ジャンル名
        "companies": [],                 # 収集済み企業リスト
        "excluded_companies": [],        # 除外された企業リスト
        "ai_results": [],                # Gemini判定全結果
        "filtered_companies": [],        # 最終絞り込み後リスト
        "collect_stats": None,           # 収集の内訳
        "sales_emails": {},              # 企業キー → 営業メール全文
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val

    # このセッションで未復元なら、保存済みの結果を一度だけ読み込む
    if not st.session_state.get("_state_loaded"):
        _load_saved_state()
        st.session_state["_state_loaded"] = True


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
# Geminiモデル名の自動解決
# ============================================================

def resolve_gemini_model_chain() -> list[str]:
    """
    APIキーで実際に generateContent が使えるモデルを、優先順に並べた
    リスト（チェーン）として返す。あるモデルが1日の無料枠を使い切った
    場合に次のモデルへ自動で切り替えるために使う。
    一度解決したらキャッシュする（事前に genai.configure 済みであること）。
    """
    global _RESOLVED_GEMINI_CHAIN
    if _RESOLVED_GEMINI_CHAIN:
        return _RESOLVED_GEMINI_CHAIN

    chain: list[str] = []
    try:
        available = [
            m.name.replace("models/", "")
            for m in genai.list_models()
            if "generateContent" in getattr(m, "supported_generation_methods", [])
        ]
        # 優先リストのうち利用可能なものを順に追加
        for pref in GEMINI_MODEL_PREFERENCES:
            if pref in available and pref not in chain:
                chain.append(pref)
        # 優先リスト外でも flash 系があれば後ろに追加（保険）
        for m in available:
            if "flash" in m and m not in chain:
                chain.append(m)
        # それでも空なら利用可能な先頭数件
        if not chain and available:
            chain = available[:3]
    except Exception:
        pass

    if not chain:
        chain = list(GEMINI_MODEL_PREFERENCES)

    _RESOLVED_GEMINI_CHAIN = chain
    return chain


def resolve_gemini_model() -> str:
    """利用可能なGeminiモデルの先頭（最優先）を返す。"""
    global _RESOLVED_GEMINI_MODEL
    if _RESOLVED_GEMINI_MODEL:
        return _RESOLVED_GEMINI_MODEL
    _RESOLVED_GEMINI_MODEL = resolve_gemini_model_chain()[0]
    return _RESOLVED_GEMINI_MODEL


def gemini_generate(model: "genai.GenerativeModel", prompt: str, max_retries: int = 5):
    """
    Geminiを呼び出す。429（無料枠の超過）が出たら以下を行う:
    - 1分あたりの上限（PerMinute）→ retry_delay 秒だけ待って再試行
    - 1日あたりの上限（PerDay）→ 別モデルへ自動で切り替えて再試行
      （各モデルは別々の1日枠を持つため）
    """
    chain = resolve_gemini_model_chain()
    current_name = str(getattr(model, "model_name", "") or "").replace("models/", "")
    current_model = model
    tried: set[str] = {current_name} if current_name else set()
    last_exc: "Exception | None" = None

    for attempt in range(max_retries):
        try:
            return current_model.generate_content(prompt)
        except Exception as exc:
            last_exc = exc
            msg = str(exc)
            is_rate_limit = (
                "429" in msg
                or "quota" in msg.lower()
                or "rate limit" in msg.lower()
                or "exceeded" in msg.lower()
            )
            if not is_rate_limit:
                raise

            is_daily = ("perday" in msg.lower()) or ("per day" in msg.lower())

            # 1日上限なら、まだ試していない別モデルへ切り替える
            if is_daily:
                next_model = next((m for m in chain if m not in tried), None)
                if next_model:
                    tried.add(next_model)
                    current_model = genai.GenerativeModel(next_model)
                    continue

            if attempt < max_retries - 1:
                match = (
                    re.search(r"retry_delay\D*(\d+)", msg)
                    or re.search(r"retry in (\d+)", msg)
                )
                wait = int(match.group(1)) + 2 if match else 8 * (attempt + 1)
                wait = min(wait, 60)
                time.sleep(wait)
                continue
            raise
    if last_exc:
        raise last_exc


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
            # 「ヒット0件」は正常な結果なので警告しない
            if "hasn't returned any results" not in data["error"]:
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
    target_years: "list[int] | None" = None,
) -> list[dict]:
    """
    選択ジャンルのキーワードを使ってSerpAPI（Google検索）で検索し、
    イベント候補（タイトル・URL・スニペット）を収集する。

    Args:
        max_queries: 使用するキーワード数の上限（APIクォータ節約）
        target_years: 検索対象の開催年リスト（未指定なら今年・来年）

    Returns:
        list of {title, url, snippet}
    """
    candidates: list[dict] = []
    seen_urls: set[str] = set()

    if not target_years:
        this_year = datetime.now().year
        target_years = [this_year, this_year + 1]
    year_part = " OR ".join(str(y) for y in target_years)

    keywords = genre.get("keywords", [])[:max_queries]

    for kw in keywords:
        # 東京近郊 + 展示会/セミナー/講演会 + 対象年 を付加して絞り込む
        query = (
            f"{kw} 東京 OR 幕張 OR 横浜 OR 川崎 OR さいたま OR 千葉 "
            f"展示会 OR セミナー OR 講演会 OR カンファレンス OR 自社イベント "
            f"{year_part}"
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


def _event_is_past(timing: str) -> bool:
    """
    開催時期の文字列から具体的な開催年月を読み取り、本日より明らかに過去なら True。
    年が読み取れない・「毎年開催」・不明などは False（＝残す）。

    例:
      "2026年1月21日～22日" → （本日2026年6月なら）True（過去）
      "2026年10月"         → False（未来）
      "毎年10月" / "不明"   → False（判定不能なので残す）
    """
    if not timing:
        return False

    text = str(timing)
    # 定期開催・通年・不明系は判定せず残す
    for kw in ("毎年", "毎月", "毎回", "定期", "通年", "随時", "不明", "未定", "TBD"):
        if kw in text:
            return False

    # 年を抽出（複数あれば最も新しい年で判定）
    years = [int(y) for y in re.findall(r"(20\d{2})", text)]
    if not years:
        return False  # 年が不明 → 残す
    year = max(years)

    # 月を抽出（最初に出てくる「○月」）
    month_match = re.search(r"(\d{1,2})\s*月", text)
    month = int(month_match.group(1)) if month_match else 12

    today = datetime.now()
    if year < today.year:
        return True
    if year == today.year and month < today.month:
        return True
    return False


def validate_events_with_gemini(
    candidates: list[dict],
    genre_label: str,
    gemini_api_key: str,
    target_years: "list[int] | None" = None,
) -> list[dict]:
    """
    Google検索で得たイベント候補をGeminiに一括送信し、
    実際の東京近郊で「これから開催される」イベントを整理・抽出したリストを返す。
    1回のAPI呼び出しで完結（レートリミット対策）。

    Returns:
        list of {name, url, venue, timing, format, snippet}
    """
    if not candidates:
        return []

    today_str = datetime.now().strftime("%Y年%m月%d日")
    if not target_years:
        this_year = datetime.now().year
        target_years = [this_year, this_year + 1]
    years_text = "・".join(f"{y}年" for y in target_years)

    # Geminiに渡す候補テキストを作成（最大40件）
    items_text = "\n".join([
        f"[{i + 1}] タイトル: {c['title']}\n    URL: {c['url']}\n    概要: {c['snippet']}"
        for i, c in enumerate(candidates[:40])
    ])

    prompt = f"""あなたは日本のイベントリサーチ専門家です。
本日は {today_str} です。
以下のGoogle検索結果から、【{genre_label}】に関連する「展示会・見本市・セミナー・講演会・カンファレンス・配信イベント」を特定し、
東京都内または東京近郊（神奈川・千葉・埼玉の東京寄りエリア）で、【本日以降に開催される未来のイベント】のみを抽出してください。

【検索結果】
{items_text}

【抽出ルール】
- 企業の製品ページ・トップページ・ニュース記事など、イベント自体でないURLは除外
- 【最重要】明らかに終了した過去のイベント（開催日が本日 {today_str} より前と確認できるもの）は除外する
- 開催年は {years_text} を中心に対象とする
- 毎年定期開催される展示会・セミナーは、次回開催が今後見込めるため含める
- 開催時期が不明・曖昧で過去か未来か判断できない場合は、念のため含めてよい（除外しすぎない）
- 同種のイベントが複数あればできるだけ多く列挙する（最大40件まで）
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
        model = genai.GenerativeModel(resolve_gemini_model())
        response = gemini_generate(model, prompt)
        parsed = _extract_json(response.text.strip())

        if isinstance(parsed, list) and parsed:
            # 必須キーが揃っており、かつ明らかに過去でないものだけ返す
            validated = []
            for item in parsed:
                if isinstance(item, dict) and item.get("name") and item.get("url"):
                    timing = item.get("timing", "不明")
                    if _event_is_past(timing):
                        continue  # 開催年月が本日より前 → 確実に除外
                    validated.append({
                        "name": item.get("name", "不明"),
                        "url": item.get("url", ""),
                        "venue": item.get("venue", "不明"),
                        "timing": timing,
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
        for c in candidates[:25]
    ]


# ============================================================
# STEP 2: 企業リスト収集
# ============================================================

_JUNK_DOMAINS = {
    "twitter.com", "x.com", "facebook.com", "instagram.com",
    "youtube.com", "linkedin.com", "line.me", "tiktok.com",
    "google.com", "google.co.jp", "apple.com", "amazon.co.jp",
    "amazon.com", "wikipedia.org",
}

_JUNK_NAME_KEYWORDS = [
    "プライバシー", "個人情報", "利用規約", "ご利用条件", "cookie",
    "Cookie", "バリアフリー", "サイトマップ", "アクセシビリティ",
    "facebook", "twitter", "instagram", "youtube", "linkedin",
    "X (formerly", "お問い合わせ", "トップへ", "ページトップ",
]

# 検索結果のページタイトルが「企業」でないと判断するためのキーワード
_PAGE_JUNK_KEYWORDS = [
    "出展のご案内", "出展案内", "出展募集", "出展のご相談", "ご案内",
    "イベント情報", "開催概要", "開催情報", "事務局", "とは",
    "一覧", "まとめ", "について", "特集", "ニュース", "プレスリリース",
    "比較", "おすすめ", "選び方", "見本市", "展示会一覧", "スケジュール",
    "来場", "チケット", "入場", "アクセス", "会場案内", "FAQ", "よくある",
]


# 「イベント・展示会の名称」を示す語（企業名ではない）
_EVENT_INDICATORS = [
    "展示会", "展示", "見本市", "博覧会", "EXPO", "Expo", "expo",
    "ウィーク", "Week", "WEEK", "フェア", "フェスタ", "フェスティバル",
    "サミット", "Summit", "フォーラム", "Forum", "商談会", "同時開催",
    "カンファレンス", "コンファレンス", "セミナー", "講演会", "シンポジウム",
    "メッセ", "ショー", "Show", "祭", "総合展",
]

# 企業（法人）であることを示す語
_COMPANY_MARKERS = [
    "株式会社", "有限会社", "合同会社", "(株)", "（株）", "(有)", "（有）",
    "一般社団法人", "公益財団法人", "協同組合",
    "Inc", "Corp", "Co.,Ltd", "Co., Ltd", "Ltd", "LLC", "K.K", "GmbH",
]


def _is_junk_page_title(title: str, event_title: str) -> bool:
    """
    検索結果のタイトル/抽出名が「企業」でない（案内・一覧・事務局・
    イベント名そのもの等）かを判定する。
    """
    if not title or len(title) < 3:
        return True
    if any(kw in title for kw in _PAGE_JUNK_KEYWORDS):
        return True

    has_company_marker = any(m in title for m in _COMPANY_MARKERS)
    if not has_company_marker:
        # 法人格がなく、イベント名を示す語を含む → 企業ではない
        if any(ind in title for ind in _EVENT_INDICATORS):
            return True
        # 法人格がなく、対象イベント名そのもの → 企業ではない
        if event_title and event_title in title:
            return True
    return False


def _is_junk_company(name: str, domain: str) -> bool:
    """SNSリンク・フッターナビ・汎用ページなど企業でないリンクを判定する。"""
    domain_lower = domain.lower()
    if any(jd in domain_lower for jd in _JUNK_DOMAINS):
        return True
    name_lower = name.lower()
    if any(kw.lower() in name_lower for kw in _JUNK_NAME_KEYWORDS):
        return True
    # 日本語・英字が1文字もない（記号・数字だけ）
    if not re.search(r"[ぁ-んァ-ンa-zA-Z一-龥]", name):
        return True
    return False


def _fetch_page_companies(event_url: str) -> list[dict]:
    """
    イベントページを直接スクレイピングし、
    主催・出展・スポンサーセクションのリンクから企業情報を収集する。
    SNS・フッターナビ・汎用ページは除外する。
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

        # フッター・ナビゲーション・SNS枠はスクレイピング対象から除外
        for tag in soup(["footer", "nav", "header"]):
            tag.decompose()

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
                        and name and len(name) >= 3
                        and not _is_junk_company(name, parsed.netloc)
                        and not _is_junk_page_title(name, "")
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


def _gather_search_results(
    event_title: str,
    serpapi_key: str,
    extra_keyword: str = "",
) -> list[dict]:
    """
    SerpAPI（Google検索）で、出展企業・出展社一覧に関する検索結果を集める。
    ここではタイトル・URL・スニペットをそのまま保持し（企業名抽出はGeminiが担当）、
    後段のGemini抽出に渡す素材を多く集めることを優先する。

    Returns:
        list of {title, link, snippet, domain}
    """
    results_out: list[dict] = []
    seen_urls: set[str] = set()

    queries = [
        f'{event_title} 出展企業 一覧',
        f'{event_title} 出展社一覧',
        f'{event_title} 出展者 リスト',
        f'{event_title} スポンサー OR 協賛企業',
        f'{event_title} 主催 OR 運営会社',
    ]
    if extra_keyword:
        queries.append(f'{event_title} {extra_keyword}')

    for query in queries[:6]:
        results = _serpapi_search(query, serpapi_key, MAX_SEARCH_RESULTS_PER_QUERY)
        for item in results:
            url = item.get("link", "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            parsed = urlparse(url)
            results_out.append({
                "title": item.get("title", ""),
                "link": url,
                "snippet": item.get("snippet", ""),
                "domain": parsed.netloc,
            })
        time.sleep(0.3)

    return results_out


def extract_companies_with_gemini(
    event_name: str,
    genre_label: str,
    search_results: list[dict],
    page_text: str,
    gemini_api_key: str,
) -> list[dict]:
    """
    検索結果のスニペットとイベントページ本文から、Geminiに
    「実在する出展・協賛・主催企業名」だけを抽出させる。
    ページタイトルをそのまま企業名にする方式の精度問題を解消する。

    Returns:
        list of {name, url, domain, source}
    """
    if not gemini_api_key:
        return []

    corpus_lines = []
    for r in search_results[:40]:
        corpus_lines.append(
            f"- タイトル: {r.get('title', '')}\n"
            f"  URL: {r.get('link', '')}\n"
            f"  概要: {r.get('snippet', '')}"
        )
    corpus = "\n".join(corpus_lines) if corpus_lines else "（検索結果なし）"
    page_excerpt = (page_text or "")[:3000]

    prompt = f"""あなたは日本の展示会・イベントのリサーチ専門家です。
イベント「{event_name}」（ジャンル: {genre_label}）に【出展・協賛・主催する実在の企業・法人】を、
できるだけ多く（最大40社）リストアップしてください。

【イベントページ本文（抜粋）】
{page_excerpt}

【Web検索結果】
{corpus}

【手順】
1. まず上記のページ本文・検索結果の中に出てくる実在企業名を拾う
2. それだけで少ない場合は、このイベント・ジャンル（{genre_label}）に
   実際に出展・協賛しそうな【実在する日本企業】をあなたの知識から補う
   （※存在しない企業を創作することは絶対に禁止。実在が確実な企業のみ）

【厳守する除外ルール】
- 企業（法人・事業者）のみを出力する
- 次は企業ではないので必ず除外:
  ・「○○展」「○○EXPO」「○○Week」「○○フェア」「見本市」「博覧会」「総合展」など展示会・イベントの名称
  ・「同時開催」「主催者一覧」「出展社一覧」「出展のご案内」などのページ名
  ・「お問い合わせ」「アクセス」「会場案内」「事務局」などのページ要素
  ・メディア記事・まとめサイト・比較サイト・行政の告知ページ
- 同じ企業の重複は1つにまとめる
- 企業名には可能なら法人格（株式会社など）を含める
- 公式サイトのURLが分かる場合のみ url に記載し、不明な場合は空文字 "" にする（推測・架空のURLは書かない）

【出力】JSON配列のみ（説明文は不要）:
[
  {{"name": "株式会社〇〇", "url": "https://example.co.jp"}},
  {{"name": "△△工業株式会社", "url": ""}}
]
"""

    try:
        genai.configure(api_key=gemini_api_key)
        model = genai.GenerativeModel(resolve_gemini_model())
        response = gemini_generate(model, prompt)
        parsed = _extract_json(response.text.strip())

        out: list[dict] = []
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict) and item.get("name"):
                    name = str(item["name"]).strip()
                    url = str(item.get("url", "") or "").strip()
                    domain = urlparse(url).netloc if url else ""
                    if (
                        name
                        and not _is_junk_page_title(name, event_name)
                        and not _is_junk_company(name, domain)
                    ):
                        out.append({
                            "name": name,
                            "url": url,
                            "domain": domain,
                            "source": "AI抽出",
                        })
        return out

    except Exception as exc:
        st.warning(f"企業名のAI抽出でエラー: {exc}")
        return []


def _normalize_company_name(name) -> str:
    """重複判定用に企業名を正規化する（法人格・空白・記号を除去）。"""
    if name is None or (isinstance(name, float) and pd.isna(name)):
        return ""
    text = str(name).strip()
    if not text or text.lower() == "nan":
        return ""
    cleaned = re.sub(
        r"(株式会社|有限会社|合同会社|一般社団法人|公益財団法人|株|\(株\)|（株）|"
        r"Co\.,?\s?Ltd\.?|Inc\.?|Corp\.?|Corporation|Company|Limited)",
        "", text, flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"[\s　・,.，。\-—|｜/]", "", cleaned)
    return cleaned.lower()


def collect_companies(
    event: dict,
    serpapi_key: str,
    gemini_api_key: str = "",
    genre_label: str = "不明",
    extra_keyword: str = "",
) -> list[dict]:
    """
    イベントに関連する実在企業を収集して返す。
    ① イベントページから直接出展リンクをスクレイピング
    ② SerpAPIで検索 → Geminiが検索結果から実在企業名を抽出
    企業名・ドメイン単位で重複除去し、各企業に event_name を付与する。
    """
    event_name = event.get("name", event.get("title", "不明"))
    event_url = event.get("url", "")

    raw: list[dict] = []

    # ① イベントページ直接スクレイピング（実URLが取れる）
    page_companies = _fetch_page_companies(event_url)
    raw.extend(page_companies)

    # ② 検索結果を集め、Geminiで実在企業名を抽出
    search_results = _gather_search_results(event_name, serpapi_key, extra_keyword)
    page_text = _scrape_company_page(event_url, max_chars=4000)
    extracted = extract_companies_with_gemini(
        event_name, genre_label, search_results, page_text, gemini_api_key
    )
    raw.extend(extracted)

    seen_names: set[str] = set()
    seen_domains: set[str] = set()
    deduped: list[dict] = []
    for c in raw:
        name = c.get("name", "")
        name_key = _normalize_company_name(name)
        domain = c.get("domain", "")
        if not name_key:
            continue
        if name_key in seen_names:
            continue
        if domain and domain in seen_domains:
            continue
        seen_names.add(name_key)
        if domain:
            seen_domains.add(domain)
        deduped.append({**c, "event_name": event_name})

    stats = {
        "page_links": len(page_companies),
        "search_results": len(search_results),
        "ai_extracted": len(extracted),
        "final": len(deduped),
    }
    return deduped, stats


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
        response = gemini_generate(model, prompt)
        parsed = _extract_json(response.text.strip())

        if not isinstance(parsed, dict):
            fallback["event_details"] = "JSONパース失敗"
            return {**company, **fallback}

        merged = {**fallback, **{k: v for k, v in parsed.items() if v is not None}}
        return {**company, **merged}

    except Exception as exc:
        fallback["event_details"] = f"APIエラー: {exc}"
        fallback["_api_error"] = str(exc)
        return {**company, **fallback}


def _company_key(c: dict) -> tuple:
    """判定済み判定用の企業キー（正規化名＋URL）。"""
    return (_normalize_company_name(c.get("name", "")), c.get("url", ""))


def _is_quota_error(msg: str) -> bool:
    m = str(msg).lower()
    return "429" in m or "quota" in m or "exceeded" in m or "rate limit" in m


def run_ai_classification(
    companies: list[dict],
    gemini_api_key: str,
    delay_seconds: int,
    genre_label: str,
    progress_bar,
    status_text,
) -> "tuple[list[dict], bool]":
    """
    企業リストを順番にGemini判定する。
    - 判定済みの社（st.session_state.ai_results）はスキップして「途中から再開」
    - 1社ごとに結果を保存（中断しても進捗が残る）
    - 無料枠（1日上限）を使い切ったら、その時点で安全に中断する

    Returns:
        (これまでの全判定結果, 無料枠切れで中断したか)
    """
    genai.configure(api_key=gemini_api_key)
    model = genai.GenerativeModel(resolve_gemini_model())

    results: list[dict] = list(st.session_state.get("ai_results", []) or [])
    done_keys = {_company_key(c) for c in results}
    total = len(companies)
    quota_stopped = False

    for i, company in enumerate(companies):
        if _company_key(company) in done_keys:
            progress_bar.progress((i + 1) / total)
            continue

        status_text.markdown(
            f"**判定中 ({i + 1} / {total}):** {company.get('name', '不明')}　"
            f"（サイト取得 → Gemini判定 → {delay_seconds}秒待機）"
        )
        result = classify_company(company, model, genre_label)

        # 無料枠切れ（クォータ）なら、そのコは保存せず安全に中断
        api_err = result.get("_api_error", "")
        if api_err and _is_quota_error(api_err):
            quota_stopped = True
            break

        result.pop("_api_error", None)
        results.append(result)
        done_keys.add(_company_key(company))

        # 逐次保存（途中で止まっても再開できるように）
        st.session_state.ai_results = results
        save_state()
        progress_bar.progress((i + 1) / total)

        if i < total - 1:
            time.sleep(delay_seconds)

    return results, quota_stopped


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


def auto_export_if_configured(
    companies: list[dict],
    cfg: dict,
    genre_label: str,
) -> "tuple[bool, str]":
    """設定が揃っていればジャンル別タブへ自動追記する。"""
    if not cfg.get("auto_sheet_export"):
        return False, "自動追記はオフです"
    if not _gs_is_configured(cfg):
        return False, "スプレッドシート連携が未設定です"
    if not companies:
        return False, "追記する企業がありません"
    sheet_tab = _genre_to_sheet_tab(genre_label)
    return export_to_google_sheets(
        companies,
        cfg["gs_spreadsheet"],
        cfg["gs_credentials"],
        sheet_tab,
    )


# ============================================================
# サイドバー
# ============================================================

def render_sidebar() -> dict:
    with st.sidebar:
        st.title("⚙️ 設定")

        st.subheader("🔑 SerpAPI（Google検索）")
        serpapi_key = st.text_input(
            "SerpAPI Key",
            value=_get_secret("SERPAPI_KEY"),
            type="password",
            help="serpapi.com で無料登録して取得（無料枠: 月100回）",
        )

        st.subheader("🤖 Gemini API")
        gemini_api_key = st.text_input(
            "Gemini API Key",
            value=_get_secret("GEMINI_API_KEY"),
            type="password",
            help="Google AI Studio（aistudio.google.com）で無料取得",
        )

        st.subheader("📊 Google連携（スプレッドシート / ドライブ）")
        gs_credentials = st.text_area(
            "サービスアカウント JSON",
            value=_get_secret("GS_CREDENTIALS_JSON"),
            height=90,
            help="JSONキーファイルの内容をそのまま貼り付け（Secrets推奨）",
            placeholder='{"type":"service_account","project_id":"..."}',
        )
        gs_spreadsheet = st.text_input(
            "スプレッドシート URL または ID",
            value=_get_secret("GS_SPREADSHEET_ID"),
            help="追記先のスプレッドシート。サービスアカウントを「編集者」で共有してください",
        )
        gs_drive_folder = st.text_input(
            "Googleドライブ バックアップフォルダ ID",
            value=_get_secret("GS_DRIVE_FOLDER_ID"),
            help="Drive上のフォルダをサービスアカウントと共有し、フォルダIDを入力",
        )

        effective_gs_credentials = _effective_gs_credentials(gs_credentials)
        effective_gs_spreadsheet = (gs_spreadsheet or "").strip() or _get_secret(
            "GS_SPREADSHEET_ID"
        )
        effective_gs_drive_folder = _normalize_drive_folder_id(
            (gs_drive_folder or "").strip() or _get_secret("GS_DRIVE_FOLDER_ID")
        )

        # 接続状態の表示（Secrets/入力が揃っているか）
        sheets_ok = bool(effective_gs_credentials and effective_gs_spreadsheet)
        drive_ok = bool(effective_gs_credentials and effective_gs_drive_folder)
        if sheets_ok:
            st.success("✅ スプレッドシート連携：設定済み（ジャンル別タブに追記可能）")
        else:
            st.warning(
                "⚠️ スプレッドシート連携：未設定\n"
                "（サービスアカウントJSON ＋ スプレッドシートURL/ID が必要）"
            )
        if drive_ok:
            st.success("✅ Googleドライブバックアップ：設定済み")
            st.caption(
                "ℹ️ 個人GoogleアカウントではDrive保存は使えません。"
                "Workspace共有ドライブのみ対応です。"
            )
        else:
            st.caption("ℹ️ ドライブバックアップ：フォルダID未設定（任意）")

        backup_status = st.session_state.get("_backup_status")
        if backup_status:
            if backup_status.startswith("✅"):
                st.caption(backup_status)
            else:
                st.warning(backup_status)

        auto_sheet_export = st.checkbox(
            "STEP3完了時にスプレッドシートへ自動追記",
            value=True,
            help="ジャンルごとに別タブを作成し、絞り込み結果を末尾に追記します",
        )
        auto_sheet_backup = st.checkbox(
            "作業内容をスプレッドシートへ自動バックアップ",
            value=True,
            help=f"同じスプレッドシートの「{BACKUP_SHEET_TAB}」タブに作業状態を保存",
        )
        auto_drive_backup = st.checkbox(
            "作業内容をGoogleドライブに自動バックアップ",
            value=False,
            help="Workspace共有ドライブのみ。個人Googleアカウントでは失敗します",
        )
        if (sheets_ok or drive_ok) and st.button(
            "☁️ 今すぐバックアップ",
            use_container_width=True,
            help="スプレッドシート（推奨）またはDriveへ手動バックアップ",
        ):
            _maybe_auto_backup(
                {
                    "auto_sheet_backup": auto_sheet_backup,
                    "auto_drive_backup": auto_drive_backup,
                    "gs_credentials": effective_gs_credentials,
                    "gs_spreadsheet": effective_gs_spreadsheet,
                    "gs_drive_folder": effective_gs_drive_folder,
                },
                force=True,
            )
            st.rerun()

        st.caption(
            "📌 営業リストはジャンル名タブ、作業バックアップは"
            f"「{BACKUP_SHEET_TAB}」タブに保存されます"
        )

        uploaded_csv = st.file_uploader(
            "📂 営業リストCSVから復元",
            type=["csv"],
            key="csv_restore_uploader",
            help="STEP4でダウンロードしたCSVをアップロードすると企業リストを復元",
        )
        if uploaded_csv is not None:
            if st.button("📂 このCSVで企業リストを復元", use_container_width=True):
                ok, msg = import_companies_from_csv(uploaded_csv)
                if ok:
                    st.success(f"✅ {msg}")
                    st.rerun()
                else:
                    st.error(f"❌ {msg}")

        st.subheader("⏱️ レートリミット設定")
        delay_seconds = st.slider(
            "Gemini API 呼び出し間隔（秒）",
            min_value=2, max_value=20, value=5,
            help="無料枠: 1分あたり15リクエスト。エラーが出たら増やしてください",
        )

        st.subheader("🔍 イベント検索設定")
        max_queries = st.slider(
            "ジャンルキーワードの使用数",
            min_value=1, max_value=5, value=5,
            help="多いほど多くのイベントを発見できますが、検索APIの消費が増えます",
        )

        st.subheader("📅 対象年（未来の開催のみ）")
        current_year = datetime.now().year
        year_options = [current_year + i for i in range(0, 4)]
        target_years = st.multiselect(
            "検索する開催年",
            options=year_options,
            default=[current_year, current_year + 1],
            help="選んだ年に開催される未来のイベントだけを抽出します（過去は除外）",
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

        st.subheader("💾 バックアップ（再デプロイ対策）")
        st.caption(
            "アプリを更新（再デプロイ）すると保存結果は初期化されます。"
            "下のボタンで作業内容をファイルに保存し、更新後にアップロードで"
            "復元できます（復元はAPIを消費しません）。"
        )
        backup_payload = json.dumps(
            {k: st.session_state.get(k) for k in PERSIST_KEYS},
            ensure_ascii=False,
        ).encode("utf-8")
        backup_name = (
            f"oshigoto_backup_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
        )
        st.download_button(
            "💾 作業内容をダウンロード保存",
            data=backup_payload,
            file_name=backup_name,
            mime="application/json",
            use_container_width=True,
        )
        uploaded_backup = st.file_uploader(
            "📂 バックアップから復元（jsonをアップロード）",
            type=["json"],
            key="backup_uploader",
        )
        if uploaded_backup is not None:
            if st.button("📂 このバックアップで復元する", use_container_width=True):
                try:
                    data = json.load(uploaded_backup)
                    for key, val in data.items():
                        if key in PERSIST_KEYS and val is not None:
                            st.session_state[key] = val
                    save_state()
                    st.success("バックアップから復元しました。")
                    st.rerun()
                except Exception as exc:
                    st.error(f"復元に失敗しました: {exc}")

        st.subheader("🗑️ データのリセット")
        st.caption(
            "リロードしても結果は保持されます。"
            "最初からやり直したいときだけ押してください。"
        )
        if st.button("結果をリセット（最初から）", use_container_width=True):
            clear_saved_state()
            st.success("保存済みの結果を消去しました。")
            st.rerun()

    return {
        "serpapi_key": serpapi_key or _get_secret("SERPAPI_KEY"),
        "gemini_api_key": gemini_api_key or _get_secret("GEMINI_API_KEY"),
        "gs_credentials": effective_gs_credentials,
        "gs_spreadsheet": effective_gs_spreadsheet,
        "gs_drive_folder": effective_gs_drive_folder,
        "auto_sheet_export": auto_sheet_export,
        "auto_sheet_backup": auto_sheet_backup,
        "auto_drive_backup": auto_drive_backup,
        "delay_seconds": delay_seconds,
        "max_queries": max_queries,
        "target_years": target_years,
        "additional_exclusions": additional_exclusions,
    }


# ============================================================
# STEP1 タブ: 完全自動イベントリサーチ
# ============================================================

def render_step1(cfg: dict) -> None:
    st.header("🔍 STEP 1 ― ジャンル選択 → 完全自動イベントリサーチ")
    st.info(
        "50カテゴリから選ぶか、自由入力欄にジャンル名（例: ゲーム）を入れて\n"
        "「自動リサーチ開始」を押すと、GoogleとGeminiが連動して\n"
        "東京都内・近郊で開催されるイベントを**全自動でリサーチ**します。"
    )

    # ① ジャンル選択（50カテゴリ）
    selected_label = st.selectbox(
        "📂 イベントジャンル（50カテゴリ）",
        options=GENRE_LABELS,
        index=0,
        help="リストから選ぶ場合はこちら。自由入力がある場合はそちらが優先されます",
    )

    # ② 自由入力ジャンル（任意・入力時はこちらを優先）
    custom_genre_input = st.text_input(
        "✏️ 自由入力ジャンル（任意）",
        placeholder="例: ゲーム / eスポーツ / 防災",
        help="50カテゴリにないジャンルを調べたいときに入力。入力するとプルダウンより優先されます",
    )

    genre, active_label = _resolve_search_genre(selected_label, custom_genre_input)
    using_custom = bool((custom_genre_input or "").strip())

    if using_custom:
        st.caption(f"🔎 自由入力 **「{active_label}」** でリサーチします（50カテゴリより優先）")

    # 検索キーワード一覧を表示
    if genre:
        source = "自由入力" if using_custom else "50カテゴリ"
        with st.expander(f"💡 「{active_label}」の検索キーワード候補（{source}）"):
            st.caption(f"上位 {cfg['max_queries']} 件のキーワードを使用します（サイドバーで変更可）")
            for i, kw in enumerate(genre["keywords"]):
                mark = "✅" if i < cfg["max_queries"] else "⬜"
                st.markdown(f"{mark} `{kw}`")

    # ③ 自動リサーチ実行ボタン
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
            st.warning("⬅️ 50カテゴリを選ぶか、自由入力欄にジャンル名を入力してください")
        else:
            st.caption(
                f"**対象ジャンル:** {active_label}\n\n"
                f"**処理内容**:\n"
                f"1. Google検索（{cfg['max_queries']}クエリ）でイベント候補を収集\n"
                f"2. Gemini APIでイベントを整理・絞り込み（1回のみ）"
            )

    if search_clicked and genre:
        if not cfg["serpapi_key"]:
            st.error("⚠️ SerpAPI Key を設定してください")
            return
        if not cfg["gemini_api_key"]:
            st.error("⚠️ Gemini API Key を設定してください（イベント整理に必要です）")
            return

        # ステップA: SerpAPI（Google検索）
        with st.spinner(f"🔍 Googleで「{active_label}」関連イベントを検索中…"):
            candidates = auto_research_events(
                genre, cfg["serpapi_key"], cfg["max_queries"], cfg["target_years"]
            )

        if not candidates:
            st.warning("Google検索でイベント候補が見つかりませんでした。キーワードやAPI設定を確認してください。")
            return

        st.caption(f"Google検索で {len(candidates)} 件の候補を取得しました → Geminiで整理中…")

        # ステップB: Gemini整理
        with st.spinner("🤖 GeminiがイベントリストをAIで整理中（1〜2分かかる場合があります）…"):
            events = validate_events_with_gemini(
                candidates, active_label, cfg["gemini_api_key"], cfg["target_years"]
            )

        st.session_state.events = events
        st.session_state.selected_genre_label = active_label
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
        if not cfg["gemini_api_key"]:
            st.error("⚠️ Gemini API Key を設定してください（企業名の抽出に必要です）")
            return

        with st.spinner("企業情報を収集中…（30秒〜1分程度かかる場合があります）"):
            raw_companies, collect_stats = collect_companies(
                event,
                cfg["serpapi_key"],
                cfg["gemini_api_key"],
                genre_label,
                extra_keyword,
            )

        valid, excluded = apply_exclusion_filter(raw_companies, cfg["additional_exclusions"])
        st.session_state.companies = valid
        st.session_state.excluded_companies = excluded
        st.session_state.collect_stats = collect_stats
        st.session_state.ai_results = []
        st.session_state.filtered_companies = []

        st.success(f"✅ 収集完了: **{len(valid)} 社**（除外: {len(excluded)} 社）")

    # 収集の内訳（どの工程で何件取れたか）を表示
    stats = st.session_state.get("collect_stats")
    if stats:
        with st.expander("🔍 収集の内訳（うまく取れない時はここを確認）", expanded=False):
            st.markdown(
                f"- イベントページから直接取得: **{stats.get('page_links', 0)} 件**\n"
                f"- Google検索のヒット件数: **{stats.get('search_results', 0)} 件**\n"
                f"- AIが検索結果から抽出した企業: **{stats.get('ai_extracted', 0)} 社**\n"
                f"- 重複除去後の最終件数: **{stats.get('final', 0)} 社**"
            )
            if stats.get("search_results", 0) == 0:
                st.warning(
                    "Google検索が0件です。SerpAPIキー、または月100回の無料枠を"
                    "使い切っていないか確認してください。"
                )
            elif stats.get("ai_extracted", 0) == 0:
                st.warning(
                    "AI抽出が0社です。Geminiのレート制限（待機後に再試行）か、"
                    "検索結果に企業名が含まれていない可能性があります。"
                )

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
        st.success(
            "✅ この企業リストは自動的に次の工程へ引き継がれます。"
            "**企業名をタップする必要はありません。**"
        )
        st.info(
            "➡️ 次の進み方：画面上部の **「🤖 STEP3: AI絞り込み」タブ** をクリックし、"
            "**「🤖 AI判定を開始する」ボタン** を押してください。"
        )


# ============================================================
# STEP3 タブ: AI絞り込み
# ============================================================

def render_step3(cfg: dict) -> None:
    st.header("🤖 STEP 3 ― AI絞り込み（Gemini API）")

    if not st.session_state.companies:
        st.info("👆 STEP2 で企業リストを収集してください")
        return

    total = len(st.session_state.companies)
    genre_label = st.session_state.get("selected_genre_label", "不明")

    # 既に判定済みの社数（中断後の再開に対応）
    company_keys = {_company_key(c) for c in st.session_state.companies}
    done_results = [
        c for c in (st.session_state.get("ai_results") or [])
        if _company_key(c) in company_keys
    ]
    done_count = len(done_results)
    remaining = total - done_count
    estimated_sec = remaining * (cfg["delay_seconds"] + 3)

    st.info(
        f"**{total} 社**を対象に以下を判定します"
        f"（判定済み {done_count} 社 / 残り {remaining} 社）。\n\n"
        f"① 東京都内・近郊エリアの企業かどうか\n"
        f"② MC・ナレーターを必要とするイベント開催実績があるか\n"
        f"③ エル・アミティエ / フェアリィと深い関係がある企業でないか\n"
        f"④ 13項目の営業情報を同時抽出\n\n"
        f"⏱️ 残りの予想所要時間: 約 **{estimated_sec // 60} 分 {estimated_sec % 60} 秒**"
        f"（{cfg['delay_seconds']} 秒間隔）\n\n"
        f"💡 途中で止まっても、もう一度ボタンを押せば**続きから再開**します"
        f"（判定済みの社は再消費しません）。"
    )

    btn_label = (
        "🤖 AI判定を開始する" if done_count == 0
        else f"▶️ 続きからAI判定を再開する（残り {remaining} 社）"
    )

    if remaining > 0 and st.button(btn_label, type="primary", use_container_width=True):
        if not cfg["gemini_api_key"]:
            st.error("⚠️ Gemini API Key を設定してください")
            return

        progress_bar = st.progress(0)
        status_text = st.empty()

        ai_results, quota_stopped = run_ai_classification(
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
        save_state()

        judged = len(ai_results)
        progress_bar.progress(1.0)

        if quota_stopped:
            status_text.markdown("⏸️ **無料枠の上限で一時停止しました**")
            st.warning(
                f"本日のGemini無料枠を使い切った可能性があります。"
                f"**ここまで {judged} 社ぶんは保存済み**です。\n\n"
                f"時間をおいて（枠リセット後に）もう一度ボタンを押すと、"
                f"**残り {total - judged} 社を続きから判定**します。"
            )
        else:
            status_text.markdown("✅ **判定完了！**")
            if filtered:
                sheet_ok, sheet_msg = auto_export_if_configured(
                    filtered, cfg, genre_label
                )
                if sheet_ok:
                    st.info(f"📊 {sheet_msg}")
                elif cfg.get("auto_sheet_export") and not _gs_is_configured(cfg):
                    st.caption(
                        "ℹ️ スプレッドシート自動追記：連携未設定のためスキップしました。"
                        "サイドバーでサービスアカウントJSONとスプレッドシートIDを設定してください。"
                    )

        tokyo_ok = sum(1 for c in ai_results if c.get("tokyo_area"))
        mc_ok = sum(1 for c in ai_results if c.get("mc_related"))
        excl = sum(1 for c in ai_results if c.get("exclusion_risk"))
        st.success(
            f"✅ {judged} 社を判定しました。\n\n"
            f"- 東京エリア該当: {tokyo_ok} 社\n"
            f"- MC・ナレーター需要あり: {mc_ok} 社\n"
            f"- 除外リスクあり（除外済み）: {excl} 社\n"
            f"- **最終残存: {len(filtered)} 社**"
        )

    elif remaining == 0 and total > 0:
        st.success(f"✅ 全 {total} 社の判定が完了しています。")

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
    st.caption("➡️ スプレッドシートに既にある場合は **STEP4 をスキップ** して **STEP5** へ進めます")

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
    st.subheader("📊 Googleスプレッドシートへ直接書き込み（手動）")
    genre_label = st.session_state.get("selected_genre_label", "不明")
    sheet_tab = _genre_to_sheet_tab(genre_label)
    st.caption(
        f"追記先タブ: **{sheet_tab}**（ジャンルごとに自動でタブを分けます）"
    )
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
                    sheet_tab,
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

    st.divider()
    st.caption("➡️ 営業メールを作成する場合は **STEP5** タブへ進んでください")


# ============================================================
# STEP5: 営業メール生成
# ============================================================

SALES_EMAIL_SUBJECT = "【MC/アナウンス業務のご提案】"
PRODUCT_SERVICE_UNKNOWN = "（不明）"

# 【 】部分のみAIが置換。それ以外は一言一句このテンプレートどおり。
SALES_EMAIL_BODY_TEMPLATE = """{company_name}
イベント・セミナーご担当者様

突然のご連絡にて失礼いたします。
フリーアナウンサーの小島瑠夏（こじま るか）と申します。

普段は大型イベント、セミナーや講演会、記者・新製品発表会、展示会をはじめ、
YouTube、トークショー、スタジアムMCなど、幅広いアナウンス業務を担当しております。

貴社のホームページを拝見し出展予定の展示会やセミナーなどで、
{product_service}の魅力を来場者へお伝えするブースMCをはじめ、
登壇者紹介や進行補助、掛け合い対応などのアナウンス業務を通じて、イベントの成功にお力添えできるのではないかと思いご連絡いたしました。

もし既にご手配済みの場合でも今後機会がございましたら、ぜひお声がけいただければ幸いです。
事務所を通さない直接契約となりますため、金額面などでも柔軟な対応が可能となっております。
まずはメールでのご相談や、15分程度のオンラインでのご挨拶だけでも大歓迎です。
ご興味をお持ちいただけましたら、まずはお気軽にご連絡いただけますと幸いです。

何卒よろしくお願い申し上げます。

━━━━━━━━━━━━━━━━━━━━━━━━━━
フリーアナウンサー
小島 瑠夏（こじま るか）

▼ 公式ホームページ
https://ruka-kojima-mc-profile.vercel.app/

▼ 料金表 / 参考動画 / ボイスサンプル
※ダウンロード不要で、そのままURLを押すだけで直接ご確認いただけます。

https://drive.google.com/drive/folders/1q2CS8DlVEjSy7_fiZ2AaDtN4yqLA3EGR

▼ お問い合わせ
Email：kojimaruka.oshigotosenyo@gmail.com
━━━━━━━━━━━━━━━━━━━━━━━━━━"""


def _company_key_str(company: dict) -> str:
    """営業メールの保存キー（JSONシリアライズ可能な文字列）。"""
    name, url = _company_key(company)
    return f"{name}|{url}"


def _sanitize_path_component(name: str, max_len: int = 80) -> str:
    """ZIP内フォルダ名用に禁止文字を除去する。"""
    s = re.sub(r'[\\/:*?"<>|]', "_", (name or "不明").strip())
    s = re.sub(r"\s+", " ", s).strip(". ")
    return (s[:max_len] if s else "不明")


def _assemble_sales_email(company_name: str, product_service: str) -> str:
    """固定テンプレートに企業名・主力製品/サービスのみ差し込んで完成メールを作る。"""
    body = SALES_EMAIL_BODY_TEMPLATE.format(
        company_name=company_name.strip(),
        product_service=product_service.strip(),
    )
    return f"件名：{SALES_EMAIL_SUBJECT}\n\n{body}"


def _research_email_placeholders(
    company: dict,
    model: "genai.GenerativeModel",
    genre_label: str,
) -> "tuple[str, str, str]":
    """
    企業HP等を参考に、【 】置換用の企業名・主力製品/サービス名を取得する。
    Returns: (company_name, product_service, error_message)
    """
    default_name = _csv_cell(company.get("name", ""), "貴社")
    url = _csv_cell(company.get("url", ""), "")
    event_name = _csv_cell(company.get("event_name", ""), "不明")
    event_details = _csv_cell(company.get("event_details", ""), "不明")
    mc_job = _csv_cell(company.get("mc_job", ""), "不明")

    page_text = (
        _scrape_company_page(url, max_chars=4000)
        if url and url != "不明"
        else ""
    )

    prompt = f"""あなたはBtoB営業メール用の企業リサーチアシスタントです。
企業ホームページの実情報に基づき、営業メールテンプレートの【 】部分に入れる値だけを調べてください。

【リスト上の情報（参考・product_service には使わない）】
- ジャンル: {genre_label}
- 企業名（リスト）: {default_name}
- 企業URL: {url}
- イベント名: {event_name}
- イベント詳細: {event_details}
- MC業務内容: {mc_job}

【ホームページ本文（product_service の唯一の根拠）】
{page_text or "（取得できませんでした）"}

【出力形式】JSONのみ（説明不要）:
{{
  "company_name": "正式な企業名（例: 株式会社スカイコム）",
  "product_service": "HP上の実在する主力製品・サービス名、または {PRODUCT_SERVICE_UNKNOWN}"
}}

【product_service の定義（最重要）】
- **ホームページ本文に実際に載っている**製品名・サービス名・ソリューション名だけを使う
- トップページや製品紹介で**いちばん強調されている主力**を選ぶ（複数なら「AやB等の○○」でつなぐ）
- **確信が持てる場合のみ**固有名詞を書く。**曖昧・推測・一般論の場合は必ず {PRODUCT_SERVICE_UNKNOWN}**
- HPが取得できない、本文から主力を特定できない、架空になりそうな場合 → **必ず {PRODUCT_SERVICE_UNKNOWN}**
- イベント情報・ジャンルからの推測は**禁止**（わからなければ {PRODUCT_SERVICE_UNKNOWN}）
- 本文「…{{product_service}}の魅力を来場者へ…」にそのまま入る句にする（20〜80文字、末尾に「。」なし）
- 「貴社のサービス」「主力ソリューション」等の抽象語だけの回答は禁止 → {PRODUCT_SERVICE_UNKNOWN}
- 御社は使わない

【company_name】
- リストの企業名が正しければそのまま使ってよい
- HPの正式表記と異なる場合はHPに合わせる
"""
    try:
        response = gemini_generate(model, prompt)
        parsed = _extract_json(response.text or "")
        if isinstance(parsed, dict):
            name = _csv_cell(parsed.get("company_name", default_name), default_name)
            product = _normalize_product_service(
                _csv_cell(parsed.get("product_service", ""), ""),
                page_text,
            )
            return name, product, ""
        return (
            default_name,
            PRODUCT_SERVICE_UNKNOWN,
            "AI応答のJSON解析に失敗しました",
        )
    except Exception as exc:
        if _is_quota_error(str(exc)):
            return "", "", str(exc)
        return (
            default_name,
            PRODUCT_SERVICE_UNKNOWN,
            str(exc),
        )


def _normalize_product_service(product: str, page_text: str) -> str:
    """HP根拠がなければ（不明）。曖昧な推測文も（不明）に統一する。"""
    if not page_text:
        return PRODUCT_SERVICE_UNKNOWN

    raw = (product or "").strip()
    if not raw or raw in ("不明", PRODUCT_SERVICE_UNKNOWN, "unknown", "N/A", "n/a"):
        return PRODUCT_SERVICE_UNKNOWN

    vague_markers = (
        "貴社のサービス",
        "貴社の主力",
        "貴社の取り組み",
        "サービス・ソリューション",
        "分野の貴社",
        "に関連する",
        "推定",
        "と思われる",
    )
    if any(marker in raw for marker in vague_markers):
        return PRODUCT_SERVICE_UNKNOWN

    return raw


def generate_sales_email_for_company(
    company: dict,
    model: "genai.GenerativeModel",
    genre_label: str,
) -> "tuple[str, str]":
    """1社分の完成メールを生成する。Returns: (email_text, error_message)"""
    company_name, product_service, err = _research_email_placeholders(
        company, model, genre_label
    )
    if err and _is_quota_error(err):
        return "", err
    if not company_name:
        return "", err or "企業名を取得できませんでした"
    return _assemble_sales_email(company_name, product_service), ""


def run_sales_email_generation(
    companies: list[dict],
    gemini_api_key: str,
    delay_seconds: int,
    genre_label: str,
    progress_bar,
    status_text,
) -> "tuple[int, bool]":
    """
    企業ごとに営業メールを生成（途中再開対応）。
    Returns: (新規生成件数, 無料枠切れで中断したか)
    """
    genai.configure(api_key=gemini_api_key)
    model = genai.GenerativeModel(resolve_gemini_model())

    if st.session_state.get("sales_emails") is None:
        st.session_state.sales_emails = {}
    emails: dict = dict(st.session_state.sales_emails)
    total = len(companies)
    quota_stopped = False
    newly_done = 0

    for i, company in enumerate(companies):
        key = _company_key_str(company)
        if key in emails and emails[key]:
            progress_bar.progress((i + 1) / total)
            continue

        status_text.markdown(
            f"**メール作成中 ({i + 1} / {total}):** {company.get('name', '不明')}　"
            f"（HP確認 → 企業名・製品名を特定 → {delay_seconds}秒待機）"
        )
        email_text, err = generate_sales_email_for_company(company, model, genre_label)

        if err and _is_quota_error(err):
            quota_stopped = True
            break
        if err:
            status_text.warning(f"⚠️ {company.get('name', '不明')}: {err}")
            progress_bar.progress((i + 1) / total)
            if i < total - 1:
                time.sleep(delay_seconds)
            continue

        emails[key] = email_text
        st.session_state.sales_emails = emails
        save_state()
        newly_done += 1
        progress_bar.progress((i + 1) / total)

        if i < total - 1:
            time.sleep(delay_seconds)

    return newly_done, quota_stopped


def build_sales_email_zip(
    companies: list[dict],
    sales_emails: dict,
    genre_label: str,
) -> bytes:
    """ジャンル/企業/営業メール.txt 構成のZIPを生成する。"""
    genre_folder = _sanitize_path_component(_genre_to_sheet_tab(genre_label))
    used_names: dict[str, int] = {}
    buf = io.BytesIO()

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for company in companies:
            key = _company_key_str(company)
            email_text = sales_emails.get(key)
            if not email_text:
                continue

            base_name = _sanitize_path_component(company.get("name", "不明"))
            count = used_names.get(base_name, 0)
            used_names[base_name] = count + 1
            folder_name = base_name if count == 0 else f"{base_name}_{count + 1}"

            arc_path = f"{genre_folder}/{folder_name}/営業メール.txt"
            info = zipfile.ZipInfo(arc_path)
            info.flag_bits |= 0x800
            zf.writestr(info, email_text.encode("utf-8"))

    return buf.getvalue()


def render_step5(cfg: dict) -> None:
    st.header("✉️ STEP 5 ― 営業メール生成（ZIPダウンロード）")
    st.info(
        "STEP3/4 で絞り込んだ企業リスト、または **CSV復元** したリストをもとに、"
        "固定テンプレートの【 】部分（企業名・主力製品/サービス）だけをAIが置き換えます。\n\n"
        f"主力製品/サービスは **企業HPに明記がある場合のみ** 自動入力します。"
        f"HPから確実に特定できない場合は **{PRODUCT_SERVICE_UNKNOWN}** と入ります（後から手動で差し替えてください）。\n\n"
        "完成後は **ジャンル/企業名/営業メール.txt** 形式のZIPを **1回** ダウンロードできます。"
    )

    if not st.session_state.filtered_companies:
        st.warning(
            "👆 先に **STEP3** で絞り込むか、サイドバーの **営業リストCSVから復元** "
            "で企業リストを読み込んでください。"
        )
        return

    companies = st.session_state.filtered_companies
    genre_label = st.session_state.get("selected_genre_label", "不明")
    if genre_label == "不明" and companies:
        genre_label = companies[0].get("genre_label", "営業リスト")

    emails: dict = st.session_state.get("sales_emails") or {}
    if not isinstance(emails, dict):
        emails = {}
        st.session_state.sales_emails = {}

    valid_companies = [
        c for c in companies
        if _csv_cell(c.get("name", ""), "").strip()
    ]
    if len(valid_companies) < len(companies):
        st.warning(
            f"⚠️ 企業名が空の行 {len(companies) - len(valid_companies)} 件をスキップします。"
            "スプレッドシートの空行を削除してからCSVを再アップロードすると安全です。"
        )
    companies = valid_companies
    if not companies:
        st.error("有効な企業名がありません。CSVの「企業名」列を確認してください。")
        return

    done_count = sum(
        1 for c in companies if emails.get(_company_key_str(c))
    )
    remaining = len(companies) - done_count

    st.success(
        f"**{len(companies)} 社**が対象です（メール作成済み **{done_count}** 社 / "
        f"残り **{remaining}** 社）"
    )
    st.caption(
        f"ジャンル: **{_genre_to_sheet_tab(genre_label)}** ／ "
        f"件名: **{SALES_EMAIL_SUBJECT}** ／ 宛名: 会社名 + イベント・セミナーご担当者様"
    )

    if remaining > 0:
        btn_label = (
            "✉️ 営業メールを一括生成する"
            if done_count == 0
            else f"▶️ 続きから営業メールを生成（残り {remaining} 社）"
        )
        if st.button(btn_label, type="primary", use_container_width=True):
            if not cfg["gemini_api_key"]:
                st.error("⚠️ Gemini API Key を設定してください")
                return

            progress_bar = st.progress(0)
            status_text = st.empty()
            newly_done, quota_stopped = run_sales_email_generation(
                companies,
                cfg["gemini_api_key"],
                cfg["delay_seconds"],
                genre_label,
                progress_bar,
                status_text,
            )
            if quota_stopped:
                st.warning(
                    "⚠️ Gemini API の無料枠上限に達したため中断しました。"
                    "時間をおいて **続きから** 再開できます。"
                )
            elif newly_done > 0:
                st.success(f"✅ {newly_done} 社分のメールを作成しました")
            st.rerun()

    emails = st.session_state.get("sales_emails") or {}
    done_count = sum(1 for c in companies if emails.get(_company_key_str(c)))

    if done_count > 0:
        st.divider()
        st.subheader("📥 ZIPダウンロード")
        genre_tab = _genre_to_sheet_tab(genre_label)
        zip_name = f"営業メール_{genre_tab}_{datetime.now().strftime('%Y%m%d_%H%M')}.zip"
        zip_bytes = build_sales_email_zip(companies, emails, genre_label)

        st.download_button(
            label=f"📦 営業メールZIPをダウンロード（{done_count} 社分）",
            data=zip_bytes,
            file_name=zip_name,
            mime="application/zip",
            use_container_width=True,
            type="primary",
        )
        st.caption(
            f"ZIPを解凍すると `{genre_tab}/企業名/営業メール.txt` の構成になります。"
            "Macのテキストエディットで開いてコピーし、メール送信にご利用ください。"
        )

        with st.expander("📋 作成済みメールのプレビュー"):
            preview_limit = 5
            shown = 0
            for company in companies:
                key = _company_key_str(company)
                text = emails.get(key)
                if not text:
                    continue
                st.markdown(f"**{company.get('name', '不明')}**")
                st.text(text[:1200] + ("…" if len(text) > 1200 else ""))
                st.divider()
                shown += 1
                if shown >= preview_limit and done_count > preview_limit:
                    st.caption(f"他 {done_count - preview_limit} 社はZIP内に含まれています")
                    break


# ============================================================
# メインエントリーポイント
# ============================================================

def main() -> None:
    st.set_page_config(
        page_title="お仕事受注企業選定ツール",
        page_icon="🎯",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    init_session_state()

    st.title("🎯 お仕事受注企業選定ツール")
    st.caption(
        "ジャンル選択 → イベント検索 → 企業収集 → AI絞り込み → CSV出力 → 営業メール生成\n"
        "エル・アミティエ・フェアリィ関連は企業段階で自動除外"
    )

    cfg = render_sidebar()

    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "🔍 STEP1: 自動イベント検索",
        "🏢 STEP2: 企業収集",
        "🤖 STEP3: AI絞り込み",
        "📤 STEP4: CSV・スプレッドシート出力",
        "✉️ STEP5: 営業メール生成",
    ])

    with tab1:
        render_step1(cfg)
    with tab2:
        render_step2(cfg)
    with tab3:
        render_step3(cfg)
    with tab4:
        render_step4(cfg)
    with tab5:
        render_step5(cfg)

    save_state()
    _maybe_auto_backup(cfg)


def _maybe_auto_backup(cfg: dict, force: bool = False) -> None:
    """内容が変わったときだけSheets/Driveへバックアップする。"""
    sheet_enabled = cfg.get("auto_sheet_backup") and _gs_is_configured(cfg)
    drive_enabled = (
        cfg.get("auto_drive_backup")
        and cfg.get("gs_credentials")
        and cfg.get("gs_drive_folder")
    )
    if not sheet_enabled and not drive_enabled:
        return
    try:
        payload = _build_backup_payload()
        content_hash = hash(payload)
        if not force and st.session_state.get("_backup_hash") == content_hash:
            return

        messages: list[str] = []
        any_ok = False

        if sheet_enabled:
            ok, msg = backup_state_to_sheet(
                cfg["gs_credentials"],
                cfg["gs_spreadsheet"],
            )
            if ok:
                any_ok = True
                messages.append(f"✅ {msg}")
            else:
                messages.append(f"⚠️ {msg}")

        if drive_enabled:
            ok, msg = backup_state_to_drive(
                cfg["gs_credentials"], cfg["gs_drive_folder"]
            )
            if ok:
                any_ok = True
                messages.append(f"✅ {msg}")
            else:
                messages.append(f"⚠️ {msg}")

        if any_ok:
            st.session_state["_backup_hash"] = content_hash
        st.session_state["_backup_status"] = "\n".join(messages)
    except Exception as exc:
        st.session_state["_backup_status"] = f"⚠️ バックアップ失敗: {exc}"


if __name__ == "__main__":
    main()
