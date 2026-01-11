from flask import Flask, request, jsonify, send_from_directory
import os
import smtplib
from email.message import EmailMessage
from datetime import datetime
import json
import uuid

from pathlib import Path
import pandas as pd
import numpy as np

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
def build_55factor_features_from_answers(answers):
    if not isinstance(answers, (list, tuple)):
        raise ValueError("answers は list か tuple で渡してください。")
    if len(answers) != 72:
        raise ValueError(f"answers の長さが 72 ではありません（len={len(answers)}）。")
    try:
        vals = [float(x) for x in answers]
    except Exception:
        raise ValueError("answers 内に数値に変換できない値があります。")

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

    dominant_axis = np.array(
        [best_row["PC1"], best_row["PC2"], best_row["PC3"], best_row["PC4"]]
    ).argmax()

    axis_to_type = {0: "S", 1: "P", 2: "C", 3: "D"}
    main_type = axis_to_type[dominant_axis]

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
        return "バランスの取れた柔軟なタイプです"
    
    if len(traits) == 1:
        return f"{traits[0]}タイプです"
    elif len(traits) == 2:
        return f"{traits[0]}タイプで、{traits[1]}傾向があります"
    else:
        return f"{traits[0]}タイプで、{traits[1]}傾向があり、{traits[2]}特徴があります"


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
    
    # デフォルト
    if not strengths:
        strengths.append("状況に応じて柔軟に対応できるバランス力がある")
    
    if not weaknesses:
        weaknesses.append("特定の状況に偏りすぎないよう意識すると良い")
    
    return {
        "strengths": strengths,
        "weaknesses": weaknesses
    }


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
# 結果保存・読み込み
# =========================
def save_result(result_data: dict) -> str:
    """結果をJSONファイルに保存し、IDを返す"""
    result_id = str(uuid.uuid4())[:8]
    result_data["id"] = result_id
    result_data["created_at"] = datetime.now().isoformat()
    
    filepath = RESULTS_DIR / f"{result_id}.json"
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(result_data, f, ensure_ascii=False, indent=2)
    
    return result_id


def load_result(result_id: str) -> dict:
    """保存された結果を読み込む"""
    filepath = RESULTS_DIR / f"{result_id}.json"
    if not filepath.exists():
        return None
    
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def list_results(company: str = None, department: str = None) -> list:
    """保存された結果一覧を取得"""
    results = []
    for filepath in RESULTS_DIR.glob("*.json"):
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
            # フィルタリング
            if company and data.get("meta", {}).get("company") != company:
                continue
            if department and data.get("meta", {}).get("department") != department:
                continue
            results.append({
                "id": data.get("id"),
                "name": data.get("meta", {}).get("name", "不明"),
                "company": data.get("meta", {}).get("company", ""),
                "department": data.get("meta", {}).get("department", ""),
                "type": data.get("result", {}).get("type", ""),
                "stress_tolerance": data.get("report", {}).get("stress_tolerance", 0),
                "created_at": data.get("created_at", ""),
            })
    
    # 新しい順にソート
    results.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return results


# =========================
# CORS 対応
# =========================
@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
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

    # 結果を保存
    save_data = {
        "meta": meta,
        "result": result,
        "report": report,
        "features_55": features_55,
        "features_4axis": raw_features_4axis,
        "answers": answers,
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
    })


@app.route("/api/results", methods=["GET"])
def api_list_results():
    """保存された結果一覧を取得"""
    company = request.args.get("company")
    department = request.args.get("department")
    results = list_results(company=company, department=department)
    return jsonify({"ok": True, "results": results})


@app.route("/api/results/<result_id>", methods=["GET"])
def api_get_result(result_id):
    """特定の結果を取得"""
    result = load_result(result_id)
    if result is None:
        return jsonify({"ok": False, "error": "結果が見つかりません"}), 404
    return jsonify({"ok": True, "data": result})


@app.route("/api/company_categories", methods=["GET"])
def api_company_categories():
    """会社カテゴリ一覧を取得"""
    return jsonify({"ok": True, "categories": COMPANY_CATEGORIES})


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
# ローカル実行用
# =========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, debug=True)
