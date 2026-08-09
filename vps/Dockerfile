FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# uid/gid を固定する。ホスト側の鍵ファイル(/etc/automation-studio/keyring.json)を
# この uid の所有にして 0600 で読ませるため、ベースイメージ更新で採番がずれると
# 起動できなくなる。暗黙の採番に頼らない。
RUN addgroup --system --gid 10001 asstudio \
 && adduser --system --uid 10001 --ingroup asstudio asstudio

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
# 運用スクリプト。import_channels.py は DB に接続するためコンテナ内で実行する
# 必要があり、手順書もそれを前提にしている。
COPY scripts ./scripts

USER asstudio

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
