from flask import Flask, request, jsonify, send_from_directory, Response
import os
import smtplib
from email.message import EmailMessage
from datetime import datetime
import json
import uuid
import csv
import io

from pathlib import Path
import pandas as pd
import numpy as np

# ===== Firebase/Firestore =====
import firebase_admin
from firebase_admin import credentials, firestore

# Firebase初期化（Cloud Run環境では自動認証）
if not firebase_admin._apps:
    firebase_admin.initialize_app()

db = firestore.client()
FIRESTORE_COLLECTION = "diagnosis_results"

# ===== Flask アプリ設定 =====
app = Flask(__name__, static_folder=".", static_url_path="")

# ===== メール設定（Cloud Run の環境変数から取得） =====
SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASS = os.environ.get("SMTP_PASS")
MAIL_FROM = os.environ.get("MAIL_FROM")
MAIL_HR = os.environ.get("MAIL_HR")

# =========================
# モデル用データの読込設定
# =========================
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "model_data"
RESULTS_DIR = BASE_DIR / "results"
RESULTS_DIR.mkdir(exist_ok=True)

# 質問ID → 因子ラベル
question_factor_df = pd.read_csv(DATA_DIR / "question_to_factor_mapping.csv")

# =========================
# 軸の日本語名マッピング
# =========================
AXIS_NAMES_JP = {
    "PC1": "和型",      # 協調性・サポート志向
    "PC2": "陽型",      # 社交性・発信力
    "PC3": "理型",      # 論理性・慎重さ
    "PC4": "導型",      # 主体性・リーダーシップ
}

AXIS_DESCRIPTIONS = {
    "PC1": "周囲との調和を大切にし、サポート役として力を発揮するタイプ",
    "PC2": "人とのコミュニケーションを楽しみ、場を盛り上げるタイプ",
    "PC3": "論理的に考え、慎重に物事を進めるタイプ",
    "PC4": "自ら率先して動き、チームを引っ張るタイプ",
}

# =========================
# 会社・所属カテゴリ
# =========================
COMPANY_CATEGORIES = [
    "さくら会在籍スタッフ",
    "中途選考",
    "歯学部生・研修医",
    "衛生士学生",
    "大学生",
    "高校生",
    "その他"
]

# =========================
# タグマスターデータ読み込み
# =========================
TAG_MASTER_PATH = DATA_DIR / "tag_master.xlsx"

def load_tag_master():
    """タグマスターをExcelから読み込み"""
    if TAG_MASTER_PATH.exists():
        try:
            df = pd.read_excel(TAG_MASTER_PATH)
            result = {}
            for category in df['category'].unique():
                result[category] = df[df['category'] == category]['value'].tolist()
            return result
        except Exception as e:
            app.logger.warning(f"tag_master.xlsx読み込みエラー: {e}")
    # デフォルト値
    return {
        "clinics": [
            "さくら歯科", "たんぽぽ歯科", "ありす歯科", "春日井きらり歯科", "松戸ありす歯科",
            "池下さくら歯科", "日進赤池たんぽぽ歯科", "春日井アップル歯科", "緑区さくら医院・歯科",
            "緑区さくら医院・医科", "緑区さくら医院・健診", "金沢さくら医院・健診", "金沢さくら医院・婦人科",
            "ハピネス歯科", "流山ハピネス歯科", "イーアス春日井", "名駅さくら医院・名古屋歯科・歯科",
            "名駅さくら医院・名古屋歯科・内科", "名駅さくら医院・名古屋歯科・皮膚科", "きらり大森歯科",
            "クローバー歯科", "流山ありす歯科・矯正歯科", "長久手さくら歯科・矯正歯科", "ヒロデンタル",
            "町屋さくら歯科・矯正歯科", "金沢さくら医院 歯科", "きた矯正歯科クリニック", "デンタルオフィス増田",
            "祥南歯科・矯正歯科医院", "三田矯正歯科医院", "グランド歯科医院", "訪問部（春日井事務所）",
            "訪問部（杁中事務所）", "春日井事務局", "名古屋駅JPタワー事務所", "いりなか事務所",
            "池下事務所", "流山事務所", "浄心事務所"
        ],
        "positions": ["歯科医師", "歯科衛生士", "歯科助手", "受付", "事務", "マネージャー"],
        "areas": [],
        "status": ["active", "leave", "retired"]
    }

def save_tag_master(tag_master: dict):
    """タグマスターをExcelに保存"""
    records = []
    for category, values in tag_master.items():
        for value in values:
            records.append({"category": category, "value": value})
    df = pd.DataFrame(records)
    df.to_excel(TAG_MASTER_PATH, index=False)

TAG_MASTER = load_tag_master()

# ステータスラベル
STATUS_LABELS = {
    "active": "在職",
    "leave": "休職",
    "retired": "退職済み"
}

# -----------------------------
# A01〜B36 → 0〜71 への index 変換
# -----------------------------
def question_id_to_index(qid: str) -> int:
    qid = str(qid).strip().upper()
    if len(qid) < 3:
        raise ValueError(f"question_id の形式が不正です: {qid}")
    prefix = qid[0]
    try:
        num = int(qid[1:])
    except ValueError:
        raise ValueError(f"question_id の番号部分が数値ではありません: {qid}")
    if not (1 <= num <= 36):
        raise ValueError(f"question_id の番号が 1〜36 の範囲外です: {qid}")
    if prefix == "A":
        return num - 1
    elif prefix == "B":
        return 36 + (num - 1)
    else:
        raise ValueError(f"question_id のプレフィックスが A/B 以外です: {qid}")


# -----------------------------
# 72問 → 55因子への変換
# -----------------------------
# 質問文改善で左右順序が逆転した質問（回答値を反転: 1↔5, 2↔4, 3→3）
# A25 (index 24), B14 (index 49), B32 (index 67)
REVERSE_QUESTION_INDICES = [24, 49, 67]

def build_55factor_features_from_answers(answers):
    if not isinstance(answers, (list, tuple)):
        raise ValueError("answers は list か tuple で渡してください。")
    if len(answers) != 72:
        raise ValueError(f"answers の長さが 72 ではありません（len={len(answers)}）。")
    try:
        vals = [float(x) for x in answers]
    except Exception:
        raise ValueError("answers 内に数値に変換できない値があります。")

    # 左右順序が逆転した質問の回答値を反転
    for idx in REVERSE_QUESTION_INDICES:
        if 0 <= idx < len(vals):
            vals[idx] = 6 - vals[idx]  # 1→5, 2→4, 3→3, 4→2, 5→1

    df = question_factor_df.copy()
    df.columns = [str(c).strip().replace("\ufeff", "") for c in df.columns]
    if "question_id" not in df.columns or "factor" not in df.columns:
        raise ValueError("question_to_factor_mapping.csv に 'question_id' または 'factor' 列がありません。")

    records = []
    for _, row in df.iterrows():
        qid = row["question_id"]
        factor = str(row["factor"]).strip()
        try:
            idx = question_id_to_index(qid)
        except Exception:
            continue
        if not (0 <= idx < len(vals)):
            continue
        try:
            v = float(vals[idx])
        except Exception:
            continue
        records.append({"factor": factor, "value": v})

    if not records:
        raise ValueError("answers と question_to_factor_mapping.csv の対応から 55因子を生成できませんでした。")

    rec_df = pd.DataFrame(records)
    factor_series = rec_df.groupby("factor")["value"].mean()
    return factor_series.to_dict()


# 旧：4軸ブロック平均
def build_features_from_answers(answers):
    if not isinstance(answers, (list, tuple)):
        raise ValueError("answers は list か tuple で渡してください。")
    if len(answers) != 72:
        raise ValueError(f"answers の長さが 72 ではありません（len={len(answers)}）。")
    try:
        vals = [float(x) for x in answers]
    except Exception:
        raise ValueError("answers 内に数値に変換できない値があります。")

    def avg(s, e):
        return sum(vals[s:e]) / (e - s)

    return {
        "ストレスに対する弱さ": avg(0, 18),
        "外向型/自問型": avg(18, 36),
        "論理重視/想い重視": avg(36, 54),
        "協調型/競争型": avg(54, 72),
    }


# モデルデータ読み込み
try:
    mean_std_df = pd.read_excel(DATA_DIR / "mean_std_55.xlsx")
    app.logger.info("mean_std_55.xlsx を読み込みました（55因子版）")
except Exception:
    mean_std_df = pd.read_excel(DATA_DIR / "mean_std_pca_features.xlsx")
    app.logger.warning("mean_std_55.xlsx が見つからないため、mean_std_pca_features.xlsx（旧4軸版）を使用します。")

try:
    pca_loadings_df = pd.read_excel(DATA_DIR / "pca_loadings_55.xlsx")
    app.logger.info("pca_loadings_55.xlsx を読み込みました（55因子版）")
except Exception:
    try:
        pca_loadings_df = pd.read_excel(DATA_DIR / "pca_loadings_clean.xlsx")
    except FileNotFoundError:
        pca_loadings_df = pd.read_excel(DATA_DIR / "pca_loadings.xlsx")

cluster_centers_df = pd.read_excel(DATA_DIR / "cluster_centers.xlsx")

# 適職参照データ読み込み（なければ初期データ作成）
JOB_FIT_FILE = DATA_DIR / "job_fit_profiles.xlsx"

def init_job_fit_data():
    """適職参照データがなければ初期データを作成"""
    if JOB_FIT_FILE.exists():
        return pd.read_excel(JOB_FIT_FILE)

    # デフォルトの職種プロファイル（PC1〜PC4の理想値）
    default_jobs = [
        {"job_name": "受付・事務", "PC1": 15.0, "PC2": 5.0, "PC3": 10.0, "PC4": -5.0, "description": "丁寧で協調性が高く、安定した対応ができる"},
        {"job_name": "歯科衛生士", "PC1": 10.0, "PC2": 8.0, "PC3": 12.0, "PC4": 5.0, "description": "患者対応と専門スキルのバランスが取れている"},
        {"job_name": "歯科医師", "PC1": 5.0, "PC2": 10.0, "PC3": 15.0, "PC4": 15.0, "description": "専門性とリーダーシップを兼ね備えている"},
        {"job_name": "歯科助手", "PC1": 18.0, "PC2": 3.0, "PC3": 8.0, "PC4": -3.0, "description": "サポート力が高く、チームワークを大切にする"},
        {"job_name": "マネージャー", "PC1": 8.0, "PC2": 12.0, "PC3": 8.0, "PC4": 18.0, "description": "人を動かし、組織を引っ張るリーダータイプ"},
        {"job_name": "カウンセラー", "PC1": 20.0, "PC2": 10.0, "PC3": 5.0, "PC4": 0.0, "description": "傾聴力が高く、人の気持ちに寄り添える"},
        {"job_name": "技工士", "PC1": 3.0, "PC2": -5.0, "PC3": 18.0, "PC4": 5.0, "description": "緻密な作業と論理的思考が得意"},
        {"job_name": "広報・マーケティング", "PC1": 5.0, "PC2": 18.0, "PC3": 10.0, "PC4": 10.0, "description": "発信力とクリエイティビティに優れる"},
    ]
    df = pd.DataFrame(default_jobs)
    df.to_excel(JOB_FIT_FILE, index=False)
    app.logger.info("適職参照データを初期作成しました: job_fit_profiles.xlsx")
    return df

job_fit_df = init_job_fit_data()

# 列名正規化
mean_std_df = mean_std_df.rename(columns={
    mean_std_df.columns[0]: "feature",
    mean_std_df.columns[1]: "mean",
    mean_std_df.columns[2]: "std",
})

pca_loadings_df = pca_loadings_df.rename(columns={
    pca_loadings_df.columns[0]: "feature",
    pca_loadings_df.columns[1]: "PC1",
    pca_loadings_df.columns[2]: "PC2",
    pca_loadings_df.columns[3]: "PC3",
    pca_loadings_df.columns[4]: "PC4",
})

cluster_centers_df = cluster_centers_df.rename(columns={
    cluster_centers_df.columns[0]: "cluster_id",
    cluster_centers_df.columns[1]: "PC1",
    cluster_centers_df.columns[2]: "PC2",
    cluster_centers_df.columns[3]: "PC3",
    cluster_centers_df.columns[4]: "PC4",
})

# PCスケール補正
PC_SCALE = {
    "PC1": 22.0,
    "PC2": 18.0,
    "PC3": 20.0,
    "PC4": 4.0,
}


# =========================
# モデル計算ヘルパー関数
# =========================
def compute_pc_and_type_from_features(raw_features: dict) -> dict:
    df = mean_std_df[["feature", "mean", "std"]].copy()
    df["value"] = df["feature"].map(raw_features)

    missing = df[df["value"].isna()]["feature"].tolist()
    if missing:
        for feat in missing:
            df.loc[df["feature"] == feat, "value"] = df.loc[df["feature"] == feat, "mean"]

    df["z"] = (df["value"] - df["mean"]) / df["std"]

    merged = pd.merge(
        df[["feature", "z"]],
        pca_loadings_df[["feature", "PC1", "PC2", "PC3", "PC4"]],
        on="feature",
        how="inner",
    )

    if merged.empty:
        raise ValueError("PCA のローディングと結合した結果が空です。feature 名の対応を確認してください。")

    pcs_raw = {}
    pcs = {}
    for pc in ["PC1", "PC2", "PC3", "PC4"]:
        raw_pc = float((merged["z"] * merged[pc]).sum())
        pcs_raw[pc] = raw_pc
        scale = PC_SCALE.get(pc, 1.0)
        pcs[pc] = raw_pc * scale

    centers = cluster_centers_df[["cluster_id", "PC1", "PC2", "PC3", "PC4"]].copy()
    diffs = centers[["PC1", "PC2", "PC3", "PC4"]] - np.array(
        [pcs["PC1"], pcs["PC2"], pcs["PC3"], pcs["PC4"]]
    )
    dists = np.sqrt((diffs ** 2).sum(axis=1))

    best_idx = int(dists.idxmin())
    best_row = centers.loc[best_idx]
    cluster_id = int(best_row["cluster_id"])

    # クラスターIDから直接タイプを決定（4タイプ均等分布）
    # クラスター0→D型, 1→S型, 2→C型, 3→P型
    cluster_to_type = {0: "D", 1: "S", 2: "C", 3: "P"}
    main_type = cluster_to_type.get(cluster_id, "S")

    return {
        "PC1": pcs["PC1"],
        "PC2": pcs["PC2"],
        "PC3": pcs["PC3"],
        "PC4": pcs["PC4"],
        "cluster_id": cluster_id,
        "type": main_type,
    }


# =========================
# レベル判定（5段階）
# =========================
def get_level_label(value: float) -> str:
    """PCスコアを5段階のラベルに変換"""
    if value >= 1.0:
        return "高い"
    elif value >= 0.4:
        return "やや高い"
    elif value <= -1.0:
        return "低い"
    elif value <= -0.4:
        return "やや低い"
    else:
        return "平均"


def get_level_index(value: float) -> int:
    """PCスコアを0-4のインデックスに変換（低い=0, 高い=4）"""
    if value >= 1.0:
        return 4
    elif value >= 0.4:
        return 3
    elif value <= -1.0:
        return 0
    elif value <= -0.4:
        return 1
    else:
        return 2


# =========================
# 回答信頼性チェック
# =========================
def calculate_consistency_score(answers: list) -> dict:
    """
    回答の一貫性をチェック
    類似質問間での回答のばらつきを検出
    """
    if not answers or len(answers) != 72:
        return {"score": 0, "details": [], "warning": "回答データが不完全です"}

    # 関連質問グループ（類似の内容を問う質問）
    related_groups = [
        # ストレス関連
        {"questions": [0, 6, 7, 8, 9, 10, 11, 12, 14, 16, 17], "name": "ストレス対処"},
        # 外向性関連
        {"questions": [18, 19, 20, 24, 26, 27], "name": "外向性"},
        # 主体性関連
        {"questions": [32, 33, 35, 36, 37, 38, 39, 40, 41], "name": "主体性"},
        # 継続性関連
        {"questions": [63, 64, 65, 66, 67, 68, 69, 70], "name": "継続性"},
    ]

    inconsistencies = []
    total_variance = 0
    group_count = 0

    for group in related_groups:
        indices = group["questions"]
        values = [answers[i] for i in indices if i < len(answers)]
        if len(values) >= 3:
            variance = np.var(values)
            total_variance += variance
            group_count += 1

            # 分散が1.5以上なら不一致の可能性
            if variance > 1.5:
                inconsistencies.append({
                    "group": group["name"],
                    "variance": round(variance, 2),
                    "message": f"{group['name']}に関する回答にばらつきがあります"
                })

    # 極端な回答パターンをチェック（全て同じ値など）
    unique_answers = len(set(answers))
    if unique_answers <= 3:
        inconsistencies.append({
            "group": "全体",
            "message": "回答パターンが極端です（ほぼ同じ値で回答されています）"
        })

    # スコア計算（0-100、高いほど一貫性あり）
    avg_variance = total_variance / group_count if group_count > 0 else 0
    base_score = max(0, 100 - (avg_variance * 30))

    # 極端な回答パターンのペナルティ
    if unique_answers <= 3:
        base_score = min(base_score, 50)

    score = int(round(base_score))

    return {
        "score": score,
        "details": inconsistencies,
        "warning": "回答に一貫性がない可能性があります" if score < 70 else None
    }


# =========================
# ストレス耐性10段階計算
# =========================
def calculate_stress_tolerance(features_55: dict, pc: dict) -> int:
    """
    55因子とPCスコアからストレス耐性を10段階で計算
    """
    # ストレス関連因子
    stress_factors = [
        "stress_tolerance",
        "stress_recovery", 
        "emotional_stability",
        "emotional_control",
        "calmness",
        "anxiety_tendency",  # 逆転項目
        "stress_sensitivity",  # 逆転項目
    ]
    
    score = 0
    count = 0
    
    for factor in stress_factors:
        if factor in features_55:
            val = features_55[factor]
            # 逆転項目は5から引く
            if factor in ["anxiety_tendency", "stress_sensitivity"]:
                val = 6 - val
            score += val
            count += 1
    
    if count > 0:
        avg = score / count
        # 1-5スケールを1-10スケールに変換
        stress_10 = int(round((avg - 1) * 2.25 + 1))
        stress_10 = max(1, min(10, stress_10))
        return stress_10
    
    return 5  # デフォルト


# =========================
# 隠れ属性バッジ生成
# =========================
def generate_badges(features_55: dict, pc: dict) -> list:
    """
    55因子とPC値から隠れ属性バッジを判定して返す
    最大5個まで、レア度順に選出
    """
    all_badges = []

    # ヘルパー関数
    def get(key, default=0):
        return features_55.get(key, default)

    # ========== カテゴリ1: 行動スタイル系 ==========

    # 🚀 スタートダッシャー（★★☆）
    if get("action_speed") >= 3.5 and get("decision_speed") >= 3.5:
        all_badges.append({
            "id": "start_dasher",
            "name": "スタートダッシャー",
            "emoji": "🚀",
            "category": "行動スタイル",
            "rarity": 2,
            "description": "即断即決で動き出すタイプ"
        })

    # 🐢 じっくり派（★★☆）
    if get("action_cautiousness") >= 3.5 and get("cautiousness") >= 3.5:
        all_badges.append({
            "id": "careful_type",
            "name": "じっくり派",
            "emoji": "🐢",
            "category": "行動スタイル",
            "rarity": 2,
            "description": "慎重に準備してから動くタイプ"
        })

    # 🎯 完遂の鬼（★★☆）
    if get("persistence") >= 3.5 and get("consistency") >= 3.5:
        all_badges.append({
            "id": "finisher",
            "name": "完遂の鬼",
            "emoji": "🎯",
            "category": "行動スタイル",
            "rarity": 2,
            "description": "一度始めたらやり抜くタイプ"
        })

    # 🦋 マルチタスカー（★★☆）
    if get("flexibility") >= 3.5 and get("activity_level") >= 3.5:
        all_badges.append({
            "id": "multitasker",
            "name": "マルチタスカー",
            "emoji": "🦋",
            "category": "行動スタイル",
            "rarity": 2,
            "description": "複数のことを同時にこなせるタイプ"
        })

    # 📐 段取りマスター（★★☆）
    if get("planning") >= 3.5 and get("conscientiousness") >= 3.5:
        all_badges.append({
            "id": "planner",
            "name": "段取りマスター",
            "emoji": "📐",
            "category": "行動スタイル",
            "rarity": 2,
            "description": "計画を立てて着実に進めるタイプ"
        })

    # ⚡ 直感アクター（★☆☆）
    if get("action_orientation") >= 3.5 and get("decision_speed") >= 3.5 and get("planning") < 3.5:
        all_badges.append({
            "id": "intuitive_actor",
            "name": "直感アクター",
            "emoji": "⚡",
            "category": "行動スタイル",
            "rarity": 1,
            "description": "考えるより先に動くタイプ"
        })

    # ========== カテゴリ2: 対人スタイル系 ==========

    # 🤝 共感マイスター（★★☆）
    if get("empathy") >= 3.5 and get("emotional_sensitivity") >= 3.5:
        all_badges.append({
            "id": "empathy_master",
            "name": "共感マイスター",
            "emoji": "🤝",
            "category": "対人スタイル",
            "rarity": 2,
            "description": "人の気持ちに寄り添えるタイプ"
        })

    # 🎤 場の盛り上げ役（★★★）
    if get("sociability") >= 3.5 and get("extroversion") >= 3.5 and get("social_ease") >= 3.5:
        all_badges.append({
            "id": "mood_maker",
            "name": "場の盛り上げ役",
            "emoji": "🎤",
            "category": "対人スタイル",
            "rarity": 3,
            "description": "ムードメーカータイプ"
        })

    # 🧘 一人時間の達人（★☆☆）
    if get("inner_world") >= 3.5 and get("extroversion") < 3.5:
        all_badges.append({
            "id": "solo_master",
            "name": "一人時間の達人",
            "emoji": "🧘",
            "category": "対人スタイル",
            "rarity": 1,
            "description": "内省・自分時間を大切にするタイプ"
        })

    # 👑 リーダー気質（★★☆）
    if get("leadership") >= 3.5 and get("self_efficacy") >= 3.5:
        all_badges.append({
            "id": "leader_type",
            "name": "リーダー気質",
            "emoji": "👑",
            "category": "対人スタイル",
            "rarity": 2,
            "description": "自然と人を引っ張るタイプ"
        })

    # 🤲 サポーター魂（★★☆）
    if get("cooperativeness") >= 3.5 and get("obedience") >= 3.0 and get("leadership") < 3.5:
        all_badges.append({
            "id": "supporter",
            "name": "サポーター魂",
            "emoji": "🤲",
            "category": "対人スタイル",
            "rarity": 2,
            "description": "縁の下の力持ちタイプ"
        })

    # 🗣️ ストレートトーカー（★☆☆）
    if get("straightforwardness") >= 3.5:
        all_badges.append({
            "id": "straight_talker",
            "name": "ストレートトーカー",
            "emoji": "🗣️",
            "category": "対人スタイル",
            "rarity": 1,
            "description": "思ったことを素直に言えるタイプ"
        })

    # ========== カテゴリ3: 思考スタイル系 ==========

    # 🔬 ロジカルシンカー（★★☆）
    if get("logical_thinking") >= 3.5 and get("cognitive_style") >= 3.5:
        all_badges.append({
            "id": "logical_thinker",
            "name": "ロジカルシンカー",
            "emoji": "🔬",
            "category": "思考スタイル",
            "rarity": 2,
            "description": "論理的に分析できるタイプ"
        })

    # 💡 アイデアマン（★★☆）
    if get("innovation_orientation") >= 3.5 and get("uniqueness") >= 3.5:
        all_badges.append({
            "id": "idea_person",
            "name": "アイデアマン",
            "emoji": "💡",
            "category": "思考スタイル",
            "rarity": 2,
            "description": "新しい発想が得意なタイプ"
        })

    # ⚖️ 正義の味方（★☆☆）
    if get("justice_sense") >= 4.0:
        all_badges.append({
            "id": "justice_seeker",
            "name": "正義の味方",
            "emoji": "⚖️",
            "category": "思考スタイル",
            "rarity": 1,
            "description": "公平さを重視するタイプ"
        })

    # 🎨 感性派（★☆☆）
    if get("emotional_sensitivity") >= 3.5 and get("sensitivity") >= 3.5 and get("logical_thinking") < 3.5:
        all_badges.append({
            "id": "sensory_type",
            "name": "感性派",
            "emoji": "🎨",
            "category": "思考スタイル",
            "rarity": 1,
            "description": "感覚や直感を大切にするタイプ"
        })

    # 🔄 柔軟シフター（★☆☆）
    if get("flexibility") >= 3.5 and get("cognitive_style") < 3.5:
        all_badges.append({
            "id": "flexible_shifter",
            "name": "柔軟シフター",
            "emoji": "🔄",
            "category": "思考スタイル",
            "rarity": 1,
            "description": "状況に応じて考えを変えられるタイプ"
        })

    # ========== カテゴリ4: メンタル特性系 ==========

    # 🛡️ 鋼のメンタル（★★★）
    if get("stress_tolerance") >= 3.5 and get("emotional_stability") >= 3.5 and get("stress_recovery") >= 3.5:
        all_badges.append({
            "id": "steel_mental",
            "name": "鋼のメンタル",
            "emoji": "🛡️",
            "category": "メンタル特性",
            "rarity": 3,
            "description": "ストレスに強いタイプ"
        })

    # 🌸 繊細さん（★★☆）
    if get("sensitivity") >= 3.5 and get("anxiety_tendency") >= 3.5:
        all_badges.append({
            "id": "sensitive_type",
            "name": "繊細さん",
            "emoji": "🌸",
            "category": "メンタル特性",
            "rarity": 2,
            "description": "感受性が豊かなタイプ"
        })

    # 🌞 ポジティブモンスター（★★★）
    if get("self_evaluation") >= 3.5 and get("self_efficacy") >= 3.5 and get("anxiety_tendency") < 3.0:
        all_badges.append({
            "id": "positive_monster",
            "name": "ポジティブモンスター",
            "emoji": "🌞",
            "category": "メンタル特性",
            "rarity": 3,
            "description": "前向きで自己肯定感が高いタイプ"
        })

    # 🔥 成長ハングリー（★★☆）
    if get("growth_motivation") >= 3.5 and get("intrinsic_motivation") >= 3.5:
        all_badges.append({
            "id": "growth_hungry",
            "name": "成長ハングリー",
            "emoji": "🔥",
            "category": "メンタル特性",
            "rarity": 2,
            "description": "自己成長への意欲が強いタイプ"
        })

    # 😌 安定志向（★★☆）
    if get("emotional_stability") >= 3.5 and get("risk_aversion") >= 3.5 and get("calmness") >= 3.5:
        all_badges.append({
            "id": "stability_seeker",
            "name": "安定志向",
            "emoji": "😌",
            "category": "メンタル特性",
            "rarity": 2,
            "description": "穏やかで安定を好むタイプ"
        })

    # 🎢 刺激シーカー（★★☆）
    if get("risk_tolerance") >= 3.5 and get("activity_level") >= 3.5 and get("risk_aversion") < 3.0:
        all_badges.append({
            "id": "thrill_seeker",
            "name": "刺激シーカー",
            "emoji": "🎢",
            "category": "メンタル特性",
            "rarity": 2,
            "description": "新しい挑戦が好きなタイプ"
        })

    # ========== カテゴリ5: 仕事観系 ==========

    # 🏆 結果にコミット（★☆☆）
    if get("value_process_vs_result") < 3.0 and get("competitiveness") >= 3.5:
        all_badges.append({
            "id": "result_oriented",
            "name": "結果にコミット",
            "emoji": "🏆",
            "category": "仕事観",
            "rarity": 1,
            "description": "結果を重視するタイプ"
        })

    # 🌱 プロセス重視（★☆☆）
    if get("value_process_vs_result") >= 3.5:
        all_badges.append({
            "id": "process_oriented",
            "name": "プロセス重視",
            "emoji": "🌱",
            "category": "仕事観",
            "rarity": 1,
            "description": "過程を大切にするタイプ"
        })

    # ⚖️ ワークライフバランサー（★☆☆）
    if get("work_life_value") >= 3.5 and get("work_life_boundary") >= 3.5:
        all_badges.append({
            "id": "work_life_balancer",
            "name": "ワークライフバランサー",
            "emoji": "⚖️",
            "category": "仕事観",
            "rarity": 1,
            "description": "仕事とプライベートを両立するタイプ"
        })

    # 💼 仕事人間（★☆☆）
    if get("work_life_value") < 3.0 and get("external_motivation") >= 3.5:
        all_badges.append({
            "id": "workaholic",
            "name": "仕事人間",
            "emoji": "💼",
            "category": "仕事観",
            "rarity": 1,
            "description": "仕事に全力投球タイプ"
        })

    # 🌟 自己実現タイプ（★☆☆）
    if get("intrinsic_motivation") >= 3.5 and get("value_self_vs_world") >= 3.5:
        all_badges.append({
            "id": "self_actualization",
            "name": "自己実現タイプ",
            "emoji": "🌟",
            "category": "仕事観",
            "rarity": 1,
            "description": "自分らしさを追求するタイプ"
        })

    # ========== カテゴリ6: 特殊バッジ（PC値ベース）==========

    pc1 = pc.get("PC1", 0)
    pc2 = pc.get("PC2", 0)
    pc3 = pc.get("PC3", 0)
    pc4 = pc.get("PC4", 0)

    # 🌈 オールラウンダー（★★★）- 全PC値がバランス
    if all(-0.8 <= v <= 0.8 for v in [pc1, pc2, pc3, pc4]):
        all_badges.append({
            "id": "all_rounder",
            "name": "オールラウンダー",
            "emoji": "🌈",
            "category": "特殊",
            "rarity": 3,
            "description": "バランス型の稀有な存在"
        })

    # ⭐ 極み人（★★★）- いずれかのPC値が突出
    if any(abs(v) > 1.5 for v in [pc1, pc2, pc3, pc4]):
        all_badges.append({
            "id": "extremist",
            "name": "極み人",
            "emoji": "⭐",
            "category": "特殊",
            "rarity": 3,
            "description": "突出した特性を持つ存在"
        })

    # 🎭 二面性の持ち主（★★★）- 対照的な特性
    if abs(pc1 - pc2) > 2.0 or abs(pc3 - pc4) > 2.0:
        all_badges.append({
            "id": "dual_nature",
            "name": "二面性の持ち主",
            "emoji": "🎭",
            "category": "特殊",
            "rarity": 3,
            "description": "対照的な特性を併せ持つ存在"
        })

    # 💎 ダイヤの原石（★★★）- リーダー素質あるが自覚なし
    if pc4 >= 1.0 and get("self_efficacy") < 3.5:
        all_badges.append({
            "id": "hidden_gem",
            "name": "ダイヤの原石",
            "emoji": "💎",
            "category": "特殊",
            "rarity": 3,
            "description": "リーダー素質があるが自覚なしタイプ"
        })

    # ========== バッジ選出 ==========

    # レア度でソート（降順）
    all_badges.sort(key=lambda x: x["rarity"], reverse=True)

    # 同カテゴリから2個以上は出さない
    selected_badges = []
    category_count = {}

    for badge in all_badges:
        cat = badge["category"]
        if category_count.get(cat, 0) < 2:
            selected_badges.append(badge)
            category_count[cat] = category_count.get(cat, 0) + 1

        if len(selected_badges) >= 5:
            break

    # レア度を星表示に変換
    rarity_display = {1: "★☆☆", 2: "★★☆", 3: "★★★"}
    for badge in selected_badges:
        badge["rarity_display"] = rarity_display.get(badge["rarity"], "★☆☆")

    return selected_badges


# =========================
# 詳細解説文生成（200文字程度、バーナム効果）
# =========================
def generate_detailed_description(pc: dict, type_label: str, features_55: dict = None) -> str:
    """
    200文字程度の詳細な性格解説を生成
    バーナム効果を意識した柔らかい表現
    """
    pc1, pc2, pc3, pc4 = pc.get("PC1", 0), pc.get("PC2", 0), pc.get("PC3", 0), pc.get("PC4", 0)

    # タイプ別ベース文
    base_texts = {
        "S": "あなたは周囲の人の気持ちを敏感に感じ取り、チームの調和を自然と意識できる方です。",
        "P": "あなたは人との関わりの中でエネルギーを得られる、コミュニケーション豊かな方です。",
        "C": "あなたは物事を論理的に分析し、着実に成果を積み上げていくことができる方です。",
        "D": "あなたは目標に向かって自ら道を切り開き、周囲を巻き込んでいく力をお持ちの方です。",
    }

    # 補足文のバリエーション
    supplements = []

    if pc1 > 0.3:
        supplements.append("人の役に立ちたいという気持ちが強く、困っている人を見過ごせない優しさがあります")
    elif pc1 < -0.3:
        supplements.append("自分の信念を大切にしながら、ぶれない軸を持って行動できます")
    else:
        supplements.append("状況に応じて柔軟に役割を変えられる適応力があります")

    if pc2 > 0.3:
        supplements.append("初対面の人とも打ち解けやすく、場を和ませる力があります")
    elif pc2 < -0.3:
        supplements.append("一人の時間を大切にし、深く考えることで良いアイデアを生み出せます")

    if pc3 > 0.3:
        supplements.append("細部まで気を配り、ミスを未然に防ぐ注意力があります")
    elif pc3 < -0.3:
        supplements.append("直感を信じて素早く行動に移せる決断力があります")

    if pc4 > 0.3:
        supplements.append("新しいことに挑戦する勇気と、それを実現させる行動力があります")
    elif pc4 < -0.3:
        supplements.append("縁の下の力持ちとして、チームを支える頼もしい存在です")

    # バーナム効果を意識した普遍的文
    universal = "時に自分の能力を過小評価してしまうこともありますが、実際には周囲から信頼されている場面も多いはずです。自分らしさを大切にしながら、得意なことを伸ばしていくことで、さらに輝けるでしょう。"

    base = base_texts.get(type_label, base_texts["S"])
    supplement_text = "。".join(supplements[:2]) + "。" if supplements else ""

    full_text = base + supplement_text + universal

    # 200文字程度に調整
    if len(full_text) > 250:
        full_text = full_text[:247] + "..."

    return full_text


# =========================
# 一言まとめ生成
# =========================
def generate_one_liner(pc: dict) -> str:
    """PCスコアから一言まとめを生成"""
    pc1, pc2, pc3, pc4 = pc["PC1"], pc["PC2"], pc["PC3"], pc["PC4"]

    traits = []

    # 高い傾向を特定
    if pc1 > 0.4:
        traits.append("周囲への気配りができる")
    if pc2 > 0.4:
        traits.append("人と接することが好き")
    if pc3 > 0.4:
        traits.append("論理的な考え方を重んじる")
    if pc4 > 0.4:
        traits.append("自ら率先して動く")

    # 低い傾向も加味
    if pc1 < -0.4:
        traits.append("自分のペースを大切にする")
    if pc2 < -0.4:
        traits.append("じっくり考えてから行動する")
    if pc3 < -0.4:
        traits.append("直感を大切にする")
    if pc4 < -0.4:
        traits.append("サポート役として力を発揮する")

    if not traits:
        # バランス型：微細な差異から特徴を抽出
        return generate_balanced_type_one_liner(pc1, pc2, pc3, pc4)

    if len(traits) == 1:
        return f"{traits[0]}タイプです"
    elif len(traits) == 2:
        return f"{traits[0]}タイプで、{traits[1]}傾向があります"
    else:
        return f"{traits[0]}タイプで、{traits[1]}傾向があり、{traits[2]}特徴があります"


def generate_balanced_type_one_liner(pc1: float, pc2: float, pc3: float, pc4: float) -> str:
    """バランス型（全ての値が±0.4以内）の場合、微細な差異から特徴を抽出"""
    pc_values = {"和": pc1, "陽": pc2, "理": pc3, "導": pc4}

    # 最も高い値と最も低い値を取得
    sorted_pcs = sorted(pc_values.items(), key=lambda x: x[1], reverse=True)
    highest = sorted_pcs[0]
    second_highest = sorted_pcs[1]
    lowest = sorted_pcs[-1]

    # 微細な傾向を表現
    trait_descriptions = {
        "和": "協調性を意識しつつ",
        "陽": "コミュニケーションを大切にしながら",
        "理": "物事を整理して考えつつ",
        "導": "主体性を持って",
    }

    balance_phrases = [
        "バランスの取れた万能型で、状況に応じて柔軟に役割を変えられます",
        "特定の偏りがなく、どんな場面にも適応できる柔軟性があります",
        "4つの特性を状況に応じて使い分けられる、適応力の高いタイプです",
    ]

    # 相対的な傾向を文章に
    if highest[1] - lowest[1] > 0.2:  # わずかでも差がある場合
        return f"{trait_descriptions[highest[0]]}全体的にバランスの取れた柔軟なタイプです"
    else:
        # 完全にフラットな場合
        import random
        return random.choice(balance_phrases)


# =========================
# 強み・弱み生成
# =========================
def generate_strengths_weaknesses(pc: dict) -> dict:
    """PCスコアから強み・弱みを生成"""
    pc1, pc2, pc3, pc4 = pc["PC1"], pc["PC2"], pc["PC3"], pc["PC4"]
    
    strengths = []
    weaknesses = []
    
    # PC1（和型）
    if pc1 > 0.4 and pc4 > 0.4:
        strengths.append("周りを巻き込みながら引っ張っていける")
    elif pc1 > 0.4:
        strengths.append("チームの雰囲気を和らげ、メンバーを支える力がある")
    
    if pc1 < -0.4:
        weaknesses.append("周囲への配慮が後回しになりやすい場面がある")
    
    # PC2（陽型）
    if pc2 > 0.4:
        strengths.append("コミュニケーション力が高く、場を明るくできる")
    if pc2 < -0.4:
        weaknesses.append("自己主張が控えめで、発言を遠慮しがち")
    
    # PC3（理型）
    if pc3 > 0.4:
        strengths.append("論理的に物事を整理し、リスクを見極められる")
    if pc3 < -0.4:
        weaknesses.append("行動が早い分、丁寧さが不足する場面がある")
    
    # PC4（導型）
    if pc4 > 0.4:
        strengths.append("主体的に動き、チームを牽引する力がある")
    if pc4 < -0.4:
        weaknesses.append("自分から前に出ることを避けがち")
    
    # 組み合わせパターン
    if pc1 > 0.4 and pc2 < -0.4:
        weaknesses.append("サポートに回りすぎて負担を抱え込みやすい")
    
    if pc3 < -0.4 and pc4 > 0.4:
        weaknesses.append("スピード重視で細部の確認が甘くなることがある")
    
    # バランス型の場合：微細な差異から強み・弱みを生成
    if not strengths or not weaknesses:
        balanced_strengths, balanced_weaknesses = generate_balanced_type_sw(pc1, pc2, pc3, pc4)
        if not strengths:
            strengths = balanced_strengths
        if not weaknesses:
            weaknesses = balanced_weaknesses

    return {
        "strengths": strengths,
        "weaknesses": weaknesses
    }


def generate_balanced_type_sw(pc1: float, pc2: float, pc3: float, pc4: float) -> tuple:
    """バランス型の強み・弱みを微細な差異から生成"""
    pc_values = [("和", pc1), ("陽", pc2), ("理", pc3), ("導", pc4)]

    # 相対的に高い順にソート
    sorted_pcs = sorted(pc_values, key=lambda x: x[1], reverse=True)
    relative_high = sorted_pcs[0][0]
    relative_low = sorted_pcs[-1][0]

    strengths = []
    weaknesses = []

    # バランス型の基本的な強み
    strengths.append("状況に応じて柔軟に対応できるバランス力がある")
    strengths.append("特定の役割に縛られず、様々な場面で力を発揮できる")

    # 相対的に高い傾向から追加の強み
    high_traits = {
        "和": "周囲との調和を自然と意識できる",
        "陽": "必要に応じて発言力を発揮できる",
        "理": "冷静に状況を分析する視点を持てる",
        "導": "必要な時には前に出る勇気がある",
    }
    strengths.append(high_traits.get(relative_high, "多角的な視点で物事を捉えられる"))

    # バランス型特有の弱み
    weaknesses.append("どの役割を担うか迷うことがある")

    # 相対的に低い傾向から弱み
    low_traits = {
        "和": "チームへの気配りをもう少し意識すると良いかも",
        "陽": "もう少し積極的に発言しても良い場面がある",
        "理": "時に立ち止まって確認する習慣があると安心",
        "導": "自分から提案する機会を増やしても良い",
    }
    weaknesses.append(low_traits.get(relative_low, "特定の強みを伸ばすことで更に輝ける"))

    return strengths, weaknesses


# =========================
# ストレス場面・対処法生成
# =========================
def generate_stress_info(pc: dict, features_55: dict) -> dict:
    """ストレスを感じやすい場面と対処法を生成"""
    pc1, pc2, pc3, pc4 = pc["PC1"], pc["PC2"], pc["PC3"], pc["PC4"]
    
    stress_situations = []
    stress_coping = []
    
    # PC1（和型）
    if pc1 > 0.4:
        stress_situations.append("対立や衝突が続く環境")
        stress_coping.append("信頼できる人と状況を共有する")
    elif pc1 < -0.4:
        stress_situations.append("過度な協調を求められる場面")
        stress_coping.append("一人で考える時間を確保する")
    
    # PC2（陽型）
    if pc2 > 0.4:
        stress_situations.append("孤立した環境での長時間作業")
        stress_coping.append("適度に人と話す機会を作る")
    elif pc2 < -0.4:
        stress_situations.append("大人数の前での発表や注目を浴びる場面")
        stress_coping.append("落ち着ける静かな場所で整理する")
    
    # PC3（理型）
    if pc3 > 0.4:
        stress_situations.append("明確な指示がなく曖昧な状況")
        stress_coping.append("情報を整理してリスト化する")
    elif pc3 < -0.4:
        stress_situations.append("細かい手順やルールに縛られる環境")
        stress_coping.append("自分なりのやり方を見つける余地を確保する")
    
    # PC4（導型）
    if pc4 > 0.4:
        stress_situations.append("決定権がなく指示待ちの状況")
        stress_coping.append("小さな範囲でも主体的に動ける部分を見つける")
    elif pc4 < -0.4:
        stress_situations.append("リーダー役を突然任される場面")
        stress_coping.append("周囲のサポートを積極的に求める")
    
    # デフォルト
    if not stress_situations:
        stress_situations.append("過度なプレッシャーがかかる状況")
    
    if not stress_coping:
        stress_coping.append("自分に合ったリフレッシュ方法を見つける")
    
    return {
        "stress_situations": stress_situations,
        "stress_coping": stress_coping
    }


# =========================
# 仕事スタイル生成
# =========================
def generate_work_style(pc: dict) -> dict:
    """仕事のスタイルを生成"""
    pc1, pc2, pc3, pc4 = pc["PC1"], pc["PC2"], pc["PC3"], pc["PC4"]
    
    work_style = {
        "collaboration": "",  # 人と進める or 一人で力を発揮
        "pace": "",           # スピード型 or 慎重型
        "approach": "",       # 主体型 or サポート型
        "thinking": "",       # 論理型 or 直感型
    }
    
    # 協働スタイル
    if pc2 > 0.3:
        work_style["collaboration"] = "人と一緒に進めることで力を発揮しやすい"
    elif pc2 < -0.3:
        work_style["collaboration"] = "一人で集中して取り組むことで力を発揮しやすい"
    else:
        work_style["collaboration"] = "状況に応じて協働も個人作業もこなせる"
    
    # ペース
    if pc3 > 0.3:
        work_style["pace"] = "慎重に確認しながら進める慎重型"
    elif pc3 < -0.3:
        work_style["pace"] = "素早く行動に移すスピード型"
    else:
        work_style["pace"] = "場面に応じてペースを調整できるタイプ"
    
    # アプローチ
    if pc4 > 0.3:
        work_style["approach"] = "自ら率先して動く主体型"
    elif pc4 < -0.3:
        work_style["approach"] = "チームを支えるサポート型"
    else:
        work_style["approach"] = "状況に応じてリードもサポートもできるタイプ"
    
    # 思考スタイル
    if pc3 > 0.3:
        work_style["thinking"] = "データや根拠を重視する論理型"
    elif pc3 < -0.3:
        work_style["thinking"] = "直感や感覚を大切にする直感型"
    else:
        work_style["thinking"] = "論理と直感のバランスが取れたタイプ"
    
    return work_style


# =========================
# 採用担当向け情報生成
# =========================
def generate_hr_insights(pc: dict, features_55: dict, stress_tolerance: int) -> dict:
    """採用担当向けの詳細情報を生成"""
    pc1, pc2, pc3, pc4 = pc["PC1"], pc["PC2"], pc["PC3"], pc["PC4"]
    
    # 良い接し方・悪い接し方
    good_approach = []
    bad_approach = []
    
    if pc1 > 0.4:
        good_approach.append("感謝の言葉をこまめに伝える")
        bad_approach.append("チームの和を乱すような指示を出す")
    elif pc1 < -0.4:
        good_approach.append("個人の裁量を尊重する")
        bad_approach.append("過度な協調を強制する")
    
    if pc2 > 0.4:
        good_approach.append("意見を聞く機会を多く設ける")
        bad_approach.append("長期間孤立させる業務を与える")
    elif pc2 < -0.4:
        good_approach.append("事前に準備時間を与えてから発言を求める")
        bad_approach.append("急に大勢の前で発表させる")
    
    if pc3 > 0.4:
        good_approach.append("明確な基準や手順を示す")
        bad_approach.append("曖昧な指示で丸投げする")
    elif pc3 < -0.4:
        good_approach.append("大まかな方向性を示して任せる")
        bad_approach.append("細かいルールで縛りすぎる")
    
    if pc4 > 0.4:
        good_approach.append("裁量権を持たせて任せる")
        bad_approach.append("全ての決定を上から押し付ける")
    elif pc4 < -0.4:
        good_approach.append("具体的な指示とサポートを提供する")
        bad_approach.append("突然リーダー役を任せる")
    
    # 気をつけて聞くべき質問
    interview_questions = []
    
    if stress_tolerance < 5:
        interview_questions.append("過去にストレスを感じた経験と、どう乗り越えたかを具体的に")
    if pc1 < -0.4:
        interview_questions.append("チームでの協力が必要だった場面での役割について")
    if pc2 < -0.4:
        interview_questions.append("人前で話すことについてどう感じるか")
    if pc4 < -0.4:
        interview_questions.append("自分から提案した経験があるか")
    
    interview_questions.append("前職（または学校）で最も困難だった状況と対処法")
    interview_questions.append("理想の上司像と、これまでの上司との関係性")
    
    # 職場適応における注意点
    adaptation_notes = []
    
    if stress_tolerance < 5:
        adaptation_notes.append("負荷が高い時期は特に声かけを意識する")
    if pc1 > 0.4:
        adaptation_notes.append("周囲への配慮で疲弊しないよう、適度に休息を促す")
    if pc2 < -0.4:
        adaptation_notes.append("発言の機会を無理に増やさず、徐々に慣れさせる")
    if pc3 > 0.4:
        adaptation_notes.append("完璧主義傾向があれば、適度な妥協点を示す")
    if pc4 < -0.4:
        adaptation_notes.append("小さな成功体験を積ませて自信をつけさせる")
    
    # 早期離職リスクサイン
    turnover_risks = []
    
    if stress_tolerance < 4:
        turnover_risks.append("表情が暗くなる、口数が減る")
    if pc1 > 0.4:
        turnover_risks.append("チーム内での孤立感を訴える")
    if pc2 > 0.4:
        turnover_risks.append("コミュニケーションを避けるようになる")
    if pc3 > 0.4:
        turnover_risks.append("細かいミスを過度に気にし始める")
    if pc4 > 0.4:
        turnover_risks.append("「やりがいがない」と漏らす")
    if pc4 < -0.4:
        turnover_risks.append("「自分には向いていない」と言い始める")
    
    turnover_risks.append("遅刻や欠勤が増える")
    turnover_risks.append("周囲との会話が減る")
    
    return {
        "good_approach": good_approach[:3],
        "bad_approach": bad_approach[:3],
        "interview_questions": interview_questions[:4],
        "adaptation_notes": adaptation_notes[:4],
        "turnover_risks": turnover_risks[:4],
    }


# =========================
# 詳細レポート生成
# =========================
def generate_type_report(pc: dict, cluster_id: int, type_label: str, features_55: dict = None) -> dict:
    """PC1〜PC4 / クラスタ / TYPE をもとに、詳細レポートを生成"""
    
    pc1 = pc.get("PC1", 0.0)
    pc2 = pc.get("PC2", 0.0)
    pc3 = pc.get("PC3", 0.0)
    pc4 = pc.get("PC4", 0.0)
    
    # 各軸のラベルとレベル
    axis_info = []
    for axis_key in ["PC1", "PC2", "PC3", "PC4"]:
        val = pc.get(axis_key, 0.0)
        axis_info.append({
            "key": axis_key,
            "name": AXIS_NAMES_JP[axis_key],
            "description": AXIS_DESCRIPTIONS[axis_key],
            "level": get_level_label(val),
            "level_index": get_level_index(val),
        })
    
    # 一言まとめ
    one_liner = generate_one_liner(pc)
    
    # 強み・弱み
    sw = generate_strengths_weaknesses(pc)
    
    # ストレス情報
    stress_info = generate_stress_info(pc, features_55 or {})
    
    # 仕事スタイル
    work_style = generate_work_style(pc)
    
    # ストレス耐性
    stress_tolerance = calculate_stress_tolerance(features_55 or {}, pc)
    
    # 採用担当向け情報
    hr_insights = generate_hr_insights(pc, features_55 or {}, stress_tolerance)

    # 詳細解説文（200文字、バーナム効果）
    detailed_description = generate_detailed_description(pc, type_label, features_55)

    # タイプ別サマリー
    type_summary_map = {
        "S": "和型（TYPE S）として、周囲との関係性を大切にしながら、チーム全体の安定に貢献しやすいタイプです。",
        "P": "陽型（TYPE P）として、人前でのコミュニケーションや情報発信で力を発揮しやすいタイプです。",
        "C": "理型（TYPE C）として、情報を整理しながらリスクを見極め、堅実に物事を進めるタイプです。",
        "D": "導型（TYPE D）として、自ら方向性を示しながらチームを牽引していくタイプです。",
    }
    summary = type_summary_map.get(
        type_label,
        "4つの特性のバランスが比較的フラットで、状況に応じて柔軟に役割を変えやすいタイプです。",
    )

    return {
        "summary": summary,
        "one_liner": one_liner,
        "detailed_description": detailed_description,
        "axis_info": axis_info,
        "strengths": sw["strengths"],
        "weaknesses": sw["weaknesses"],
        "stress_situations": stress_info["stress_situations"],
        "stress_coping": stress_info["stress_coping"],
        "work_style": work_style,
        "stress_tolerance": stress_tolerance,
        "hr_insights": hr_insights,
        "cluster_id": int(cluster_id),
        "type": type_label,
    }


# =========================
# 適職マッチング機能
# =========================
def calculate_job_fit(pc: dict, top_n: int = 3) -> list:
    """
    ユーザーのPCスコアと各職種の理想プロファイルを比較し、
    マッチ度の高い順にベスト職種を返す
    """
    global job_fit_df

    if job_fit_df is None or job_fit_df.empty:
        return []

    user_pc = np.array([pc.get("PC1", 0), pc.get("PC2", 0), pc.get("PC3", 0), pc.get("PC4", 0)])

    results = []
    for _, row in job_fit_df.iterrows():
        job_pc = np.array([row.get("PC1", 0), row.get("PC2", 0), row.get("PC3", 0), row.get("PC4", 0)])

        # ユークリッド距離を計算
        distance = np.sqrt(np.sum((user_pc - job_pc) ** 2))

        # 距離を0-100のマッチ度スコアに変換（距離が小さいほど高スコア）
        # 最大距離を約50と仮定
        match_score = max(0, min(100, 100 - (distance * 2)))

        results.append({
            "job_name": row.get("job_name", "不明"),
            "match_score": round(match_score, 1),
            "description": row.get("description", ""),
            "distance": round(distance, 2)
        })

    # マッチ度でソート
    results.sort(key=lambda x: x["match_score"], reverse=True)

    return results[:top_n]


def reload_job_fit_data():
    """適職データをリロード"""
    global job_fit_df
    if JOB_FIT_FILE.exists():
        job_fit_df = pd.read_excel(JOB_FIT_FILE)
    return job_fit_df


# =========================
# 結果保存・読み込み
# =========================
def save_result(result_data: dict) -> str:
    """結果をFirestoreに保存し、IDを返す"""
    result_id = str(uuid.uuid4())[:8]
    result_data["id"] = result_id
    result_data["created_at"] = datetime.now().isoformat()

    # Firestore用にデータ整形
    doc_data = {
        "id": result_id,
        "answers_raw": result_data.get("answers", []),
        "factors_55": result_data.get("features_55", {}),
        "factors_55_z": {},
        "flags": {
            "is_valid": True,
            "is_complete": True,
            "data_version": "3.0",
            "is_test": False
        },
        "meta": result_data.get("meta", {}),
        "pc_values": {
            "PC1": result_data.get("result", {}).get("PC1", 0),
            "PC2": result_data.get("result", {}).get("PC2", 0),
            "PC3": result_data.get("result", {}).get("PC3", 0),
            "PC4": result_data.get("result", {}).get("PC4", 0),
        },
        "report": result_data.get("report", {}),
        "result": result_data.get("result", {}),
        "consistency": result_data.get("consistency", {}),
        "badges": result_data.get("badges", []),
        "created_at": firestore.SERVER_TIMESTAMP,
        "updated_at": firestore.SERVER_TIMESTAMP,
    }

    # Firestoreに保存
    db.collection(FIRESTORE_COLLECTION).document(result_id).set(doc_data)

    return result_id


def load_result(result_id: str) -> dict:
    """Firestoreから結果を読み込む"""
    doc = db.collection(FIRESTORE_COLLECTION).document(result_id).get()
    if not doc.exists:
        return None
    return doc.to_dict()


def list_results(company: str = None, department: str = None, status: str = None,
                  clinic: str = None, position: str = None, area: str = None) -> list:
    """Firestoreから結果一覧を取得（拡張フィルタリング対応）"""
    results = []

    # Firestoreからデータ取得
    query = db.collection(FIRESTORE_COLLECTION)

    # 基本的なフィルタリング（Firestoreで可能なもの）
    # 注意: Firestoreは複合クエリに制限があるため、一部はPython側でフィルタリング

    docs = query.stream()

    for doc in docs:
        data = doc.to_dict()
        meta = data.get("meta", {})
        tags = meta.get("tags", {})

        # Python側でフィルタリング
        if company and meta.get("company") != company:
            continue
        if department and meta.get("department") != department:
            continue
        if status and meta.get("status") != status:
            continue
        if clinic and clinic not in tags.get("clinics", []):
            continue
        if position and position not in tags.get("positions", []):
            continue
        if area and area not in tags.get("areas", []):
            continue

        result_obj = data.get("result", {})

        # created_atの変換（Firestore Timestampの場合）
        created_at = data.get("created_at", "")
        if hasattr(created_at, 'isoformat'):
            created_at = created_at.isoformat()
        elif hasattr(created_at, 'timestamp'):
            created_at = datetime.fromtimestamp(created_at.timestamp()).isoformat()

        results.append({
            "id": data.get("id"),
            "name": meta.get("name", "不明"),
            "email": meta.get("email", ""),
            "company": meta.get("company", ""),
            "department": meta.get("department", ""),
            "tags": tags,
            "status": meta.get("status", "active"),
            "type": result_obj.get("type", ""),
            "result": result_obj,  # PC1〜PC4を含むresultオブジェクト
            "meta": meta,
            "stress_tolerance": data.get("report", {}).get("stress_tolerance", 0),
            "consistency_score": data.get("consistency", {}).get("score", None),
            "created_at": created_at,
        })

    # 新しい順にソート
    results.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return results


def update_result_meta(result_id: str, updates: dict) -> bool:
    """Firestoreのmeta情報を更新"""
    doc_ref = db.collection(FIRESTORE_COLLECTION).document(result_id)
    doc = doc_ref.get()

    if not doc.exists:
        return False

    # metaフィールドを更新
    update_data = {"updated_at": firestore.SERVER_TIMESTAMP}
    for key, value in updates.items():
        if key in ["name", "email", "company", "department", "tags", "status"]:
            update_data[f"meta.{key}"] = value

    doc_ref.update(update_data)
    return True


# =========================
# CORS 対応
# =========================
@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    return response


# =========================
# API エンドポイント
# =========================

@app.get("/health")
def health():
    return "ok", 200


@app.get("/")
def index():
    return send_from_directory(".", "sakura_diagnosis.html")


@app.get("/admin")
def admin():
    return send_from_directory(".", "sakura_admin.html")


@app.get("/reference-admin")
def reference_admin():
    return send_from_directory(".", "sakura_reference_data_admin.html")


@app.get("/job-fit-admin")
def job_fit_admin():
    return send_from_directory(".", "sakura_job_fit_admin.html")


@app.route("/api/sakura_psych", methods=["POST", "OPTIONS"])
def api_sakura_psych():
    """診断API"""
    if request.method == "OPTIONS":
        return ("", 204)

    data = request.get_json(silent=True) or {}
    meta = data.get("meta", {})

    raw_features_4axis = None
    features_55 = None

    answers = data.get("answers")
    if isinstance(answers, (list, tuple)):
        try:
            answers = [float(x) for x in answers]
        except Exception:
            return jsonify({
                "ok": False,
                "error": "answers は数値リストで渡してください。"
            }), 400

        try:
            raw_features_4axis = build_features_from_answers(answers)
        except Exception as e:
            app.logger.warning("4軸指標の計算に失敗: %s", e)

        try:
            features_55 = build_55factor_features_from_answers(answers)
        except Exception as e:
            return jsonify({
                "ok": False,
                "error": f"answers から55因子への変換でエラー: {str(e)}"
            }), 400

    if features_55 is None:
        maybe_features = data.get("features")
        if isinstance(maybe_features, dict):
            features_55 = maybe_features

    if features_55 is None:
        return jsonify({
            "ok": False,
            "error": "answers（72回答）または features（55因子dict）を渡してください。"
        }), 400

    try:
        result = compute_pc_and_type_from_features(features_55)
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": f"モデル計算中にエラー: {str(e)}"
        }), 400

    report = generate_type_report(
        pc={
            "PC1": result["PC1"],
            "PC2": result["PC2"],
            "PC3": result["PC3"],
            "PC4": result["PC4"],
        },
        cluster_id=result["cluster_id"],
        type_label=result["type"],
        features_55=features_55,
    )

    # 回答信頼性チェック
    consistency = calculate_consistency_score(answers) if answers else {"score": None}

    # metaにデフォルトのステータスを設定
    if "status" not in meta:
        meta["status"] = "active"

    # 適職ベスト3を計算
    job_fit = calculate_job_fit({
        "PC1": result["PC1"],
        "PC2": result["PC2"],
        "PC3": result["PC3"],
        "PC4": result["PC4"],
    }, top_n=3)

    # 隠れ属性バッジを生成
    badges = generate_badges(features_55, {
        "PC1": result["PC1"],
        "PC2": result["PC2"],
        "PC3": result["PC3"],
        "PC4": result["PC4"],
    })

    # 結果を保存
    save_data = {
        "meta": meta,
        "result": result,
        "report": report,
        "features_55": features_55,
        "features_4axis": raw_features_4axis,
        "answers": answers,
        "consistency": consistency,
        "job_fit": job_fit,
        "badges": badges,
    }
    result_id = save_result(save_data)

    return jsonify({
        "ok": True,
        "result_id": result_id,
        "result": result,
        "report": report,
        "features_55": features_55,
        "features_4axis": raw_features_4axis,
        "meta": meta,
        "consistency": consistency,
        "job_fit": job_fit,
        "badges": badges,
    })


@app.route("/api/results", methods=["GET"])
def api_list_results():
    """保存された結果一覧を取得（拡張フィルタリング対応）"""
    company = request.args.get("company")
    department = request.args.get("department")
    status = request.args.get("status")
    clinic = request.args.get("clinic")
    position = request.args.get("position")
    area = request.args.get("area")
    results = list_results(
        company=company,
        department=department,
        status=status,
        clinic=clinic,
        position=position,
        area=area
    )
    return jsonify({"ok": True, "results": results})


@app.route("/api/results/export", methods=["GET"])
def api_export_results():
    """結果をCSV形式でエクスポート"""
    company = request.args.get("company")
    status = request.args.get("status")
    results = list_results(company=company, status=status)

    output = io.StringIO()
    writer = csv.writer(output)

    # ヘッダー
    writer.writerow([
        "ID", "名前", "メール", "所属カテゴリ", "部署", "クリニック", "職種", "エリア",
        "ステータス", "タイプ", "ストレス耐性", "信頼性スコア", "診断日時"
    ])

    # データ
    for r in results:
        tags = r.get("tags", {})
        writer.writerow([
            r.get("id", ""),
            r.get("name", ""),
            r.get("email", ""),
            r.get("company", ""),
            r.get("department", ""),
            ", ".join(tags.get("clinics", [])),
            ", ".join(tags.get("positions", [])),
            ", ".join(tags.get("areas", [])),
            STATUS_LABELS.get(r.get("status", "active"), r.get("status", "")),
            r.get("type", ""),
            r.get("stress_tolerance", ""),
            r.get("consistency_score", ""),
            r.get("created_at", ""),
        ])

    output.seek(0)
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=sakura_results.csv"}
    )


@app.route("/api/results/<result_id>", methods=["GET"])
def api_get_result(result_id):
    """特定の結果を取得"""
    result = load_result(result_id)
    if result is None:
        return jsonify({"ok": False, "error": "結果が見つかりません"}), 404
    return jsonify({"ok": True, "data": result})


@app.route("/api/results/<result_id>/meta", methods=["PUT", "OPTIONS"])
def api_update_result_meta(result_id):
    """結果のmeta情報を更新"""
    if request.method == "OPTIONS":
        return ("", 204)

    data = request.get_json(silent=True) or {}
    success = update_result_meta(result_id, data)

    if not success:
        return jsonify({"ok": False, "error": "結果が見つかりません"}), 404

    return jsonify({"ok": True, "message": "更新しました"})


@app.route("/api/results/<result_id>/status", methods=["PUT", "OPTIONS"])
def api_update_result_status(result_id):
    """結果のステータスを更新"""
    if request.method == "OPTIONS":
        return ("", 204)

    data = request.get_json(silent=True) or {}
    new_status = data.get("status")

    if new_status not in ["active", "leave", "retired"]:
        return jsonify({"ok": False, "error": "無効なステータスです"}), 400

    success = update_result_meta(result_id, {"status": new_status})

    if not success:
        return jsonify({"ok": False, "error": "結果が見つかりません"}), 404

    return jsonify({"ok": True, "message": "ステータスを更新しました"})


@app.route("/api/company_categories", methods=["GET"])
def api_company_categories():
    """会社カテゴリ一覧を取得"""
    return jsonify({"ok": True, "categories": COMPANY_CATEGORIES})


@app.route("/api/tag_master", methods=["GET"])
def api_get_tag_master():
    """タグマスター一覧を取得"""
    global TAG_MASTER
    TAG_MASTER = load_tag_master()  # 最新を読み込み
    return jsonify({"ok": True, "tags": TAG_MASTER, "status_labels": STATUS_LABELS})


@app.route("/api/tag_master", methods=["PUT", "OPTIONS"])
def api_update_tag_master():
    """タグマスターを更新"""
    if request.method == "OPTIONS":
        return ("", 204)

    global TAG_MASTER
    data = request.get_json(silent=True) or {}

    if "tags" in data:
        TAG_MASTER = data["tags"]
        save_tag_master(TAG_MASTER)

    return jsonify({"ok": True, "message": "タグマスターを更新しました"})


# =========================
# 参照データ管理API
# =========================
@app.route("/api/reference-data", methods=["GET"])
def api_get_reference_data():
    """mean_std_55のデータを取得"""
    data = mean_std_df.to_dict('records')
    return jsonify({"ok": True, "data": data})


@app.route("/api/reference-data/<feature>", methods=["PUT", "OPTIONS"])
def api_update_reference_data(feature):
    """特定の特徴量のmean/stdを更新"""
    if request.method == "OPTIONS":
        return ("", 204)

    global mean_std_df

    data = request.get_json(silent=True) or {}
    new_mean = data.get("mean")
    new_std = data.get("std")

    if new_mean is None or new_std is None:
        return jsonify({"ok": False, "error": "mean と std が必要です"}), 400

    # DataFrameを更新
    mask = mean_std_df["feature"] == feature
    if not mask.any():
        return jsonify({"ok": False, "error": f"特徴量 '{feature}' が見つかりません"}), 404

    mean_std_df.loc[mask, "mean"] = float(new_mean)
    mean_std_df.loc[mask, "std"] = float(new_std)

    # Excelに保存
    mean_std_df.to_excel(DATA_DIR / "mean_std_55.xlsx", index=False)

    return jsonify({"ok": True, "message": f"'{feature}' を更新しました"})


@app.route("/api/reference-data", methods=["POST", "OPTIONS"])
def api_add_reference_data():
    """新しい特徴量を追加"""
    if request.method == "OPTIONS":
        return ("", 204)

    global mean_std_df

    data = request.get_json(silent=True) or {}
    feature = data.get("feature")
    new_mean = data.get("mean")
    new_std = data.get("std")

    if not feature or new_mean is None or new_std is None:
        return jsonify({"ok": False, "error": "feature, mean, std が必要です"}), 400

    # 重複チェック
    if feature in mean_std_df["feature"].values:
        return jsonify({"ok": False, "error": f"特徴量 '{feature}' は既に存在します"}), 400

    # 追加
    new_row = pd.DataFrame([{"feature": feature, "mean": float(new_mean), "std": float(new_std)}])
    mean_std_df = pd.concat([mean_std_df, new_row], ignore_index=True)

    # Excelに保存
    mean_std_df.to_excel(DATA_DIR / "mean_std_55.xlsx", index=False)

    return jsonify({"ok": True, "message": f"'{feature}' を追加しました"})


@app.route("/api/reference-data/<feature>", methods=["DELETE", "OPTIONS"])
def api_delete_reference_data(feature):
    """特徴量を削除"""
    if request.method == "OPTIONS":
        return ("", 204)

    global mean_std_df

    mask = mean_std_df["feature"] == feature
    if not mask.any():
        return jsonify({"ok": False, "error": f"特徴量 '{feature}' が見つかりません"}), 404

    mean_std_df = mean_std_df[~mask].reset_index(drop=True)

    # Excelに保存
    mean_std_df.to_excel(DATA_DIR / "mean_std_55.xlsx", index=False)

    return jsonify({"ok": True, "message": f"'{feature}' を削除しました"})


# =========================
# 適職データ管理API
# =========================
@app.route("/api/job-fit-profiles", methods=["GET"])
def api_list_job_fit_profiles():
    """適職プロファイル一覧を取得"""
    global job_fit_df
    job_fit_df = reload_job_fit_data()

    if job_fit_df is None or job_fit_df.empty:
        return jsonify({"ok": True, "profiles": []})

    profiles = job_fit_df.to_dict(orient="records")
    return jsonify({"ok": True, "profiles": profiles})


@app.route("/api/job-fit-profiles/<job_name>", methods=["PUT", "OPTIONS"])
def api_update_job_fit_profile(job_name):
    """適職プロファイルを更新"""
    if request.method == "OPTIONS":
        return ("", 204)

    global job_fit_df

    data = request.get_json(silent=True) or {}

    mask = job_fit_df["job_name"] == job_name
    if not mask.any():
        return jsonify({"ok": False, "error": f"職種 '{job_name}' が見つかりません"}), 404

    # 更新
    for col in ["PC1", "PC2", "PC3", "PC4", "description"]:
        if col in data:
            job_fit_df.loc[mask, col] = data[col]

    # Excelに保存
    job_fit_df.to_excel(JOB_FIT_FILE, index=False)

    return jsonify({"ok": True, "message": f"'{job_name}' を更新しました"})


@app.route("/api/job-fit-profiles", methods=["POST", "OPTIONS"])
def api_add_job_fit_profile():
    """新しい適職プロファイルを追加"""
    if request.method == "OPTIONS":
        return ("", 204)

    global job_fit_df

    data = request.get_json(silent=True) or {}
    job_name = data.get("job_name")

    if not job_name:
        return jsonify({"ok": False, "error": "job_name が必要です"}), 400

    # 重複チェック
    if job_name in job_fit_df["job_name"].values:
        return jsonify({"ok": False, "error": f"職種 '{job_name}' は既に存在します"}), 400

    # 追加
    new_row = pd.DataFrame([{
        "job_name": job_name,
        "PC1": float(data.get("PC1", 0)),
        "PC2": float(data.get("PC2", 0)),
        "PC3": float(data.get("PC3", 0)),
        "PC4": float(data.get("PC4", 0)),
        "description": data.get("description", ""),
    }])
    job_fit_df = pd.concat([job_fit_df, new_row], ignore_index=True)

    # Excelに保存
    job_fit_df.to_excel(JOB_FIT_FILE, index=False)

    return jsonify({"ok": True, "message": f"'{job_name}' を追加しました"})


@app.route("/api/job-fit-profiles/<job_name>", methods=["DELETE", "OPTIONS"])
def api_delete_job_fit_profile(job_name):
    """適職プロファイルを削除"""
    if request.method == "OPTIONS":
        return ("", 204)

    global job_fit_df

    mask = job_fit_df["job_name"] == job_name
    if not mask.any():
        return jsonify({"ok": False, "error": f"職種 '{job_name}' が見つかりません"}), 404

    job_fit_df = job_fit_df[~mask].reset_index(drop=True)

    # Excelに保存
    job_fit_df.to_excel(JOB_FIT_FILE, index=False)

    return jsonify({"ok": True, "message": f"'{job_name}' を削除しました"})


# =========================
# タグ別統計API
# =========================
@app.route("/api/tag-statistics", methods=["GET"])
def api_tag_statistics():
    """タグ別の平均PCスコアを計算して返す"""
    # 全結果を読み込み
    all_results = []
    for filepath in RESULTS_DIR.glob("*.json"):
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
            result = data.get("result", {})
            meta = data.get("meta", {})
            tags = meta.get("tags", {})

            # PCスコアがある結果のみ
            if result.get("PC1") is not None:
                all_results.append({
                    "PC1": result.get("PC1", 0),
                    "PC2": result.get("PC2", 0),
                    "PC3": result.get("PC3", 0),
                    "PC4": result.get("PC4", 0),
                    "company": meta.get("company", ""),
                    "tags": tags,
                })

    if not all_results:
        return jsonify({"ok": True, "statistics": {}, "total_count": 0})

    # タグマスターを取得
    tag_master = load_tag_master()

    statistics = {}

    # カテゴリ別に集計
    for category, values in tag_master.items():
        if category == "status":
            continue  # ステータスは除外

        statistics[category] = {}

        for tag_value in values:
            # このタグを持つ結果をフィルタ
            matching = []
            for r in all_results:
                tag_list = r["tags"].get(category, [])
                if tag_value in tag_list:
                    matching.append(r)

            if matching:
                avg_pc1 = sum(r["PC1"] for r in matching) / len(matching)
                avg_pc2 = sum(r["PC2"] for r in matching) / len(matching)
                avg_pc3 = sum(r["PC3"] for r in matching) / len(matching)
                avg_pc4 = sum(r["PC4"] for r in matching) / len(matching)

                statistics[category][tag_value] = {
                    "count": len(matching),
                    "PC1": round(avg_pc1, 2),
                    "PC2": round(avg_pc2, 2),
                    "PC3": round(avg_pc3, 2),
                    "PC4": round(avg_pc4, 2),
                }

    # 所属カテゴリ別も集計
    statistics["company"] = {}
    for category in COMPANY_CATEGORIES:
        matching = [r for r in all_results if r["company"] == category]
        if matching:
            avg_pc1 = sum(r["PC1"] for r in matching) / len(matching)
            avg_pc2 = sum(r["PC2"] for r in matching) / len(matching)
            avg_pc3 = sum(r["PC3"] for r in matching) / len(matching)
            avg_pc4 = sum(r["PC4"] for r in matching) / len(matching)

            statistics["company"][category] = {
                "count": len(matching),
                "PC1": round(avg_pc1, 2),
                "PC2": round(avg_pc2, 2),
                "PC3": round(avg_pc3, 2),
                "PC4": round(avg_pc4, 2),
            }

    return jsonify({
        "ok": True,
        "statistics": statistics,
        "total_count": len(all_results)
    })


@app.route("/api/tag-statistics/import-to-profiles", methods=["POST", "OPTIONS"])
def api_import_tag_to_profiles():
    """タグの統計値を適職プロファイルにインポート"""
    if request.method == "OPTIONS":
        return ("", 204)

    global job_fit_df

    data = request.get_json(silent=True) or {}
    tag_name = data.get("tag_name")
    pc1 = data.get("PC1", 0)
    pc2 = data.get("PC2", 0)
    pc3 = data.get("PC3", 0)
    pc4 = data.get("PC4", 0)
    count = data.get("count", 0)

    if not tag_name:
        return jsonify({"ok": False, "error": "tag_name が必要です"}), 400

    description = f"タグ「{tag_name}」の平均値（{count}名）"

    # 既存チェック
    if tag_name in job_fit_df["job_name"].values:
        # 更新
        mask = job_fit_df["job_name"] == tag_name
        job_fit_df.loc[mask, "PC1"] = pc1
        job_fit_df.loc[mask, "PC2"] = pc2
        job_fit_df.loc[mask, "PC3"] = pc3
        job_fit_df.loc[mask, "PC4"] = pc4
        job_fit_df.loc[mask, "description"] = description
        message = f"'{tag_name}' を更新しました"
    else:
        # 追加
        new_row = pd.DataFrame([{
            "job_name": tag_name,
            "PC1": pc1,
            "PC2": pc2,
            "PC3": pc3,
            "PC4": pc4,
            "description": description,
        }])
        job_fit_df = pd.concat([job_fit_df, new_row], ignore_index=True)
        message = f"'{tag_name}' を追加しました"

    # Excelに保存
    job_fit_df.to_excel(JOB_FIT_FILE, index=False)

    return jsonify({"ok": True, "message": message})


# =========================
# メール送信
# =========================
def send_mail(to_addrs, subject, body):
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS):
        app.logger.warning("SMTP settings are not configured. Skip sending email.")
        return

    if isinstance(to_addrs, str):
        to_addrs = [to_addrs]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM or SMTP_USER
    msg["To"] = ", ".join(to_addrs)
    msg.set_content(body)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.starttls()
        smtp.login(SMTP_USER, SMTP_PASS)
        smtp.send_message(msg)


@app.post("/api/send-email")
def api_send_email():
    """診断結果をメール送信"""
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"success": False, "error": "invalid json"}), 400

    result_id = data.get("result_id")
    email = data.get("email")

    if not email:
        return jsonify({"success": False, "error": "email is required"}), 400

    if not result_id:
        return jsonify({"success": False, "error": "result_id is required"}), 400

    result_data = load_result(result_id)
    if not result_data:
        return jsonify({"success": False, "error": "結果が見つかりません"}), 404

    meta = result_data.get("meta", {})
    name = meta.get("name", "受検者")
    report = result_data.get("report", {})
    result = result_data.get("result", {})

    type_label = result.get("type", "")
    type_name = f"タイプ{type_label}"
    
    # 軸情報
    axis_info = report.get("axis_info", [])
    axis_text = "\n".join([
        f"・{a['name']}（{a['description'][:10]}...）: {a['level']}"
        for a in axis_info
    ])

    user_subject = f"[さくら会] 心理診断結果のご案内"
    user_body = f"""{name} 様

この度は心理診断にご協力いただき、ありがとうございます。
あなたの診断結果は以下の通りです。

■ 診断タイプ
{report.get('summary', '')}

■ 一言でまとめると
{report.get('one_liner', '')}

■ 各軸の傾向
{axis_text}

■ 強み
{"、".join(report.get('strengths', []))}

■ 注意点
{"、".join(report.get('weaknesses', []))}

■ ストレス耐性
{report.get('stress_tolerance', 5)}点（10点満点）

※ 本診断は、さくら会内での配置・育成・1on1面談に活用するためのものであり、
   合否や評価を直接決めるものではありません。

医療法人さくら会
"""

    try:
        send_mail(email, user_subject, user_body)
        if MAIL_HR:
            hr_subject = f"[さくら会] {name} さんの心理診断結果"
            send_mail(MAIL_HR, hr_subject, user_body)
    except Exception as e:
        app.logger.exception("send_mail failed: %s", e)

    return jsonify({"success": True})


# =========================
# 相性診断API
# =========================

# タイプ相性マトリクス（基本スコア）
# D: 導型, S: 和型, C: 理型, P: 陽型
TYPE_COMPATIBILITY_MATRIX = {
    ("D", "D"): 70,  # 同タイプ: 似すぎて衝突しやすい
    ("D", "S"): 85,  # 導×和: リーダーとサポーターで好相性
    ("D", "C"): 75,  # 導×理: 論理的な補佐役として機能
    ("D", "P"): 60,  # 導×陽: 両方主張が強く衝突しやすい
    ("S", "D"): 85,
    ("S", "S"): 75,  # 同タイプ: 穏やかだが決断力に欠ける
    ("S", "C"): 90,  # 和×理: 最も安定した組み合わせ
    ("S", "P"): 70,  # 和×陽: 陽がリードしすぎる傾向
    ("C", "D"): 75,
    ("C", "S"): 90,
    ("C", "C"): 70,  # 同タイプ: 慎重すぎて進まない
    ("C", "P"): 80,  # 理×陽: 分析と発信のバランス
    ("P", "D"): 60,
    ("P", "S"): 70,
    ("P", "C"): 80,
    ("P", "P"): 65,  # 同タイプ: 競争的になりやすい
}

# タイプ相性コメント
TYPE_COMPATIBILITY_COMMENTS = {
    ("D", "D"): {
        "summary": "リーダー同士の組み合わせ",
        "strengths": ["決断力が高く、物事を推進できる", "目標達成への意識が強い"],
        "challenges": ["主導権争いになりやすい", "意見が対立した際に譲り合いが難しい"],
        "advice": "役割分担を明確にし、それぞれの担当領域を尊重することが大切です"
    },
    ("D", "S"): {
        "summary": "リーダーとサポーターの理想的な組み合わせ",
        "strengths": ["導型がリードし、和型がフォロー", "チームとしてバランスが取れる"],
        "challenges": ["和型の意見が埋もれやすい"],
        "advice": "導型は和型の意見を積極的に聞く姿勢を持ちましょう"
    },
    ("D", "C"): {
        "summary": "決断力と分析力の組み合わせ",
        "strengths": ["導型の決断を理型が論理的に補佐", "計画と実行のバランス"],
        "challenges": ["理型の慎重さが導型には遅く感じることも"],
        "advice": "理型の分析を尊重しつつ、スピード感も意識しましょう"
    },
    ("D", "P"): {
        "summary": "二人とも前に出たいタイプ",
        "strengths": ["エネルギッシュで推進力がある", "積極的に物事を進められる"],
        "challenges": ["主張がぶつかりやすい", "譲り合いが難しい場面も"],
        "advice": "お互いの強みを認め合い、競争より協力を意識しましょう"
    },
    ("S", "S"): {
        "summary": "穏やかで協調的な組み合わせ",
        "strengths": ["争いが少なく穏やかな関係", "互いを尊重し合える"],
        "challenges": ["決断が遅くなりがち", "リーダーシップが不足することも"],
        "advice": "どちらかが意識的に決断役を担うと良いでしょう"
    },
    ("S", "C"): {
        "summary": "最も安定感のある組み合わせ",
        "strengths": ["和型の協調性と理型の正確さが噛み合う", "安定したチームワーク"],
        "challenges": ["変化への対応が遅くなる可能性"],
        "advice": "お互いの強みを活かし、安定感を武器にしましょう"
    },
    ("S", "P"): {
        "summary": "サポーターと発信者の組み合わせ",
        "strengths": ["陽型が引っ張り、和型がフォロー", "外向的なバランス"],
        "challenges": ["陽型のペースに和型が合わせすぎることも"],
        "advice": "和型も自分の意見を伝えることを意識しましょう"
    },
    ("C", "C"): {
        "summary": "分析派同士の組み合わせ",
        "strengths": ["論理的で正確な判断ができる", "ミスが少ない"],
        "challenges": ["慎重になりすぎて進まない", "決断に時間がかかる"],
        "advice": "ある程度で判断を下すタイミングを決めておきましょう"
    },
    ("C", "P"): {
        "summary": "分析力と発信力の組み合わせ",
        "strengths": ["理型の分析を陽型が発信", "内容と伝え方のバランス"],
        "challenges": ["アプローチの違いで意見が分かれることも"],
        "advice": "理型は発信を、陽型は深掘りを互いに学び合いましょう"
    },
    ("P", "P"): {
        "summary": "社交的なエネルギッシュコンビ",
        "strengths": ["明るく活気のある雰囲気", "発信力・影響力が強い"],
        "challenges": ["競争的になりやすい", "地道な作業が疎かになることも"],
        "advice": "役割を分けて、お互いの活躍の場を作りましょう"
    },
}


def calculate_compatibility(data1: dict, data2: dict) -> dict:
    """
    2人の診断結果から相性を計算する

    Args:
        data1: 1人目の診断データ（result, meta, reportを含む）
        data2: 2人目の診断データ

    Returns:
        相性スコアと詳細情報
    """
    result1 = data1.get("result", {})
    result2 = data2.get("result", {})

    type1 = result1.get("type", "S")
    type2 = result2.get("type", "S")

    # タイプ相性の基本スコア
    type_score = TYPE_COMPATIBILITY_MATRIX.get((type1, type2), 70)

    # PCスコアの距離計算
    pc1 = np.array([
        result1.get("PC1", 0),
        result1.get("PC2", 0),
        result1.get("PC3", 0),
        result1.get("PC4", 0)
    ])
    pc2 = np.array([
        result2.get("PC1", 0),
        result2.get("PC2", 0),
        result2.get("PC3", 0),
        result2.get("PC4", 0)
    ])

    distance = float(np.sqrt(np.sum((pc1 - pc2) ** 2)))

    # 距離による調整（近いほどプラス、遠いほどマイナス）
    # 距離0で+20、距離100で-20
    adjustment = max(-20, min(20, (50 - distance) / 2.5))

    # 最終スコア
    final_score = type_score + adjustment
    final_score = max(0, min(100, final_score))

    # 相性コメント取得
    comment_key = (type1, type2)
    if comment_key not in TYPE_COMPATIBILITY_COMMENTS:
        comment_key = (type2, type1)

    comments = TYPE_COMPATIBILITY_COMMENTS.get(comment_key, {
        "summary": "相性情報",
        "strengths": [],
        "challenges": [],
        "advice": ""
    })

    # スコアに応じたラベル
    if final_score >= 85:
        score_label = "とても良い"
        score_emoji = "💕"
    elif final_score >= 75:
        score_label = "良い"
        score_emoji = "😊"
    elif final_score >= 65:
        score_label = "普通"
        score_emoji = "🙂"
    elif final_score >= 55:
        score_label = "やや注意"
        score_emoji = "🤔"
    else:
        score_label = "要配慮"
        score_emoji = "⚠️"

    return {
        "score": round(final_score, 1),
        "score_label": score_label,
        "score_emoji": score_emoji,
        "type_score": type_score,
        "distance": round(distance, 2),
        "adjustment": round(adjustment, 1),
        "type1": type1,
        "type2": type2,
        "comments": comments
    }


@app.route("/api/compatibility", methods=["POST", "OPTIONS"])
def api_compatibility():
    """2人の診断結果の相性を計算"""
    if request.method == "OPTIONS":
        return "", 204

    try:
        body = request.get_json()
        id1 = body.get("id1")
        id2 = body.get("id2")

        if not id1 or not id2:
            return jsonify({"ok": False, "error": "id1とid2が必要です"}), 400

        if id1 == id2:
            return jsonify({"ok": False, "error": "同じ人は比較できません"}), 400

        # Firestoreからデータ取得
        doc1 = db.collection(FIRESTORE_COLLECTION).document(id1).get()
        doc2 = db.collection(FIRESTORE_COLLECTION).document(id2).get()

        if not doc1.exists:
            return jsonify({"ok": False, "error": f"ID {id1} が見つかりません"}), 404
        if not doc2.exists:
            return jsonify({"ok": False, "error": f"ID {id2} が見つかりません"}), 404

        data1 = doc1.to_dict()
        data2 = doc2.to_dict()

        # 相性計算
        compatibility = calculate_compatibility(data1, data2)

        # レスポンスに人物情報も含める
        meta1 = data1.get("meta", {})
        meta2 = data2.get("meta", {})
        result1 = data1.get("result", {})
        result2 = data2.get("result", {})
        report1 = data1.get("report", {})
        report2 = data2.get("report", {})

        return jsonify({
            "ok": True,
            "compatibility": compatibility,
            "person1": {
                "id": id1,
                "name": meta1.get("name", "不明"),
                "type": result1.get("type", "S"),
                "type_name": AXIS_NAMES_JP.get(f"PC{['D','S','C','P'].index(result1.get('type', 'S'))+1}", ""),
                "PC1": result1.get("PC1", 0),
                "PC2": result1.get("PC2", 0),
                "PC3": result1.get("PC3", 0),
                "PC4": result1.get("PC4", 0),
                "axis_info": report1.get("axis_info", []),
                "stress_tolerance": report1.get("stress_tolerance", 5)
            },
            "person2": {
                "id": id2,
                "name": meta2.get("name", "不明"),
                "type": result2.get("type", "S"),
                "type_name": AXIS_NAMES_JP.get(f"PC{['D','S','C','P'].index(result2.get('type', 'S'))+1}", ""),
                "PC1": result2.get("PC1", 0),
                "PC2": result2.get("PC2", 0),
                "PC3": result2.get("PC3", 0),
                "PC4": result2.get("PC4", 0),
                "axis_info": report2.get("axis_info", []),
                "stress_tolerance": report2.get("stress_tolerance", 5)
            }
        })

    except Exception as e:
        app.logger.exception("compatibility API error: %s", e)
        return jsonify({"ok": False, "error": str(e)}), 500


# =========================
# ローカル実行用
# =========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=True)
