# さくら会 心理診断システム

## 概要
72問の心理診断テストを通じて、受検者の特性を4つの軸（和型・陽型・理型・導型）で分析し、採用・配置・育成に活用できるシステムです。

## ファイル構成
```
sakura_psych_api/
├── main.py                 # Flaskアプリケーション本体
├── sakura_diagnosis.html   # 受検者向け診断画面
├── sakura_admin.html       # 採用担当者向け管理画面
├── requirements.txt        # Python依存関係
├── Dockerfile              # Cloud Run用Dockerfile
├── model_data/             # モデルデータ
│   ├── cluster_centers.xlsx
│   ├── mean_std_55.xlsx
│   ├── mean_std_pca_features.xlsx
│   ├── pca_loadings.xlsx
│   ├── pca_loadings_55.xlsx
│   └── question_to_factor_mapping.csv
└── results/                # 診断結果保存ディレクトリ（自動作成）
```

## 機能一覧

### 受検者向け画面（`/`）
- 72問の心理診断テスト
- 4つの特性（和型・陽型・理型・導型）の分析
- 強み・弱みの表示
- ストレスを感じやすい場面と対処法
- 仕事のスタイル分析
- ストレス耐性（10段階）

### 管理画面（`/admin`）
- 診断結果一覧（所属・名前でフィルタリング可能）
- 各受検者の詳細情報
  - ストレス耐性（10段階）
  - 良い接し方・悪い接し方
  - 気をつけて聞くべき質問
  - 職場適応における注意点
  - 早期離職のリスクサイン
- ChatGPT用プロンプト生成（相性診断用）

## デプロイ方法（Cloud Run）

### 1. Google Cloud SDKのインストールと認証
```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
```

### 2. コンテナイメージのビルドとプッシュ
```bash
cd sakura_psych_api

# Artifact Registry にリポジトリを作成（初回のみ）
gcloud artifacts repositories create sakura-repo \
    --repository-format=docker \
    --location=asia-northeast1

# ビルドとプッシュ
gcloud builds submit --tag asia-northeast1-docker.pkg.dev/YOUR_PROJECT_ID/sakura-repo/sakura-psych-api
```

### 3. Cloud Run にデプロイ
```bash
gcloud run deploy sakura-psych-api \
    --image asia-northeast1-docker.pkg.dev/YOUR_PROJECT_ID/sakura-repo/sakura-psych-api \
    --platform managed \
    --region asia-northeast1 \
    --allow-unauthenticated \
    --set-env-vars "SMTP_HOST=smtp.gmail.com,SMTP_PORT=587,SMTP_USER=your-email@gmail.com,SMTP_PASS=your-app-password,MAIL_FROM=さくら会 <your-email@gmail.com>,MAIL_HR=hr@sakura-kai.jp"
```

## 環境変数（メール送信用・オプション）

| 変数名 | 説明 | 例 |
|--------|------|-----|
| SMTP_HOST | SMTPサーバー | smtp.gmail.com |
| SMTP_PORT | SMTPポート | 587 |
| SMTP_USER | SMTPユーザー | your-email@gmail.com |
| SMTP_PASS | SMTPパスワード（アプリパスワード） | xxxx-xxxx-xxxx-xxxx |
| MAIL_FROM | 送信元表示名 | さくら会 <your-email@gmail.com> |
| MAIL_HR | 管理者通知先 | hr@sakura-kai.jp |

※ Gmailを使用する場合は、アプリパスワードを生成してください。

## ローカル実行

```bash
cd sakura_psych_api
pip install -r requirements.txt
python main.py
```
ブラウザで http://localhost:8080 にアクセス

## API エンドポイント

| エンドポイント | メソッド | 説明 |
|---------------|---------|------|
| `/` | GET | 受検者向け診断画面 |
| `/admin` | GET | 管理画面 |
| `/api/sakura_psych` | POST | 診断実行API |
| `/api/results` | GET | 結果一覧取得 |
| `/api/results/<id>` | GET | 特定結果取得 |
| `/api/company_categories` | GET | 所属カテゴリ一覧 |
| `/api/send-email` | POST | 結果メール送信 |
| `/health` | GET | ヘルスチェック |

## 注意事項
- 診断結果は`results/`ディレクトリにJSONファイルとして保存されます
- Cloud Runの場合、コンテナ再起動で結果が消えるため、永続化が必要な場合はCloud Storageや Cloud SQLとの連携を検討してください
