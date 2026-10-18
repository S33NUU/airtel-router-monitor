FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY router_monitor.py .

# Create config from example if not provided
COPY router_monitor_config.example.json ./router_monitor_config.example.json

# Volume for config and persistent data
VOLUME /app/data

# Environment variables (override via docker run/env file)
ENV ROUTER_IP=192.168.1.1
ENV ROUTER_USERNAME=admin
ENV ROUTER_PASSWORD=changeme
ENV TELEGRAM_BOT_TOKEN=changeme
ENV TELEGRAM_CHAT_ID=changeme

# Run the monitor
CMD ["python3", "router_monitor.py"]
