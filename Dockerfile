# 洞真（dongzhen）指纹探针服务镜像。
# 镜像名：ghcr.io/qq33357486/dongzhen（生产只拉镜像，不在服务器上构建）。
# 本地构建：docker build -t dongzhen:service .
FROM python:3.12-slim

WORKDIR /app

# 先装依赖（利用层缓存）。注意：COPY 多源到同一目录时按 basename 落盘，
# 两个 requirements.txt 同名会互相覆盖，故 api 的那份改名拷入。
COPY requirements.txt ./
COPY api/requirements.txt ./api-requirements.txt
RUN pip install --no-cache-dir -r api-requirements.txt -r requirements.txt

# 指纹管线源码 + 服务代码 + 统一库
COPY fingerprint.py bank_builder.py challenge_suite.py enrollment.py ./
COPY api/ ./api/
COPY data/unified_bank.json data/unified_bank.json

ENV DZ_BANK_PATH=/app/data/unified_bank.json \
    DZ_HISTORY_DB=/app/data/history/dongzhen_history.sqlite3 \
    DZ_HOST=0.0.0.0 \
    DZ_PORT=8100

EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8100/health', timeout=4).status==200 else 1)"

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8100"]
