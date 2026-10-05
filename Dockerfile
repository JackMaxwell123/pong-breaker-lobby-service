FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY rendezvous.py ./
USER 10001:10001
EXPOSE 8765
CMD ["python", "rendezvous.py", "--host", "0.0.0.0"]
