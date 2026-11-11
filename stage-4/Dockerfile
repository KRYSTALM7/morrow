FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
WORKDIR /app
COPY app.py /app/app.py
COPY ai_agent.py finance_store.py gemini_provider.py payments_store.py paypal.py forecast_engine.py /app/
COPY wallet_store.py /app/wallet_store.py
COPY web /app/web
RUN mkdir -p /data && chown 10001:10001 /data
ENV PAYPAL_DB_PATH=/data/paypal.sqlite3
ENV MORROW_WALLET_DB_PATH=/data/paypal-wallet.sqlite3
VOLUME ["/data"]
EXPOSE 8080
USER 10001:10001
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2).read()"]
CMD ["python", "/app/app.py"]
