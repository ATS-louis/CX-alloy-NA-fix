FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY cxalloy_na_fix.py app.py ./
EXPOSE 8000
# Binds to the platform-assigned $PORT (Render/Railway/Cloud Run), else 8000.
# Set APP_PASSWORD in the platform's environment settings to require a password.
CMD gunicorn -b 0.0.0.0:${PORT:-8000} -w 2 --timeout 120 app:app
