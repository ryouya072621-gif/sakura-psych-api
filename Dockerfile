FROM python:3.11-slim

WORKDIR /app

# 依存関係をインストール
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# アプリケーションファイルをコピー
COPY main.py .
COPY sakura_diagnosis.html .
COPY sakura_admin.html .
COPY model_data/ ./model_data/

# 結果保存用ディレクトリを作成
RUN mkdir -p results

# ポート設定
ENV PORT=8080
EXPOSE 8080

# アプリケーション起動
CMD ["python", "main.py"]
