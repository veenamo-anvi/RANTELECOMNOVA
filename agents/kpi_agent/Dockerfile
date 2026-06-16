FROM python:3.12-slim
WORKDIR /app
# CPU-only Torch as a separate cached layer (spec H.2)
RUN pip install --no-cache-dir torch>=2.2.0 --index-url https://download.pytorch.org/whl/cpu
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "-u", "kpi_agent.py"]
