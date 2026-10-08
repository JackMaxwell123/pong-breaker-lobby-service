FROM python:3.12-slim-bookworm
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libfontconfig1 libgl1 libx11-6 libxcursor1 libxinerama1 libxrandr2 libxi6 libasound2 libpulse0 libdbus-1-3 libstdc++6 && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY requirements-progression.txt ./
RUN pip install --no-cache-dir -r requirements-progression.txt
COPY rendezvous.py economy.py progression.py replay_verifier.py store_verification.py ./
COPY public_resources.py ./
COPY public ./public
COPY verifier ./verifier
COPY install_verifier.py ./
RUN python install_verifier.py --destination /opt/pong-verifier
ENV PB_GODOT_BINARY=/opt/pong-verifier/godot
ENV PB_REPLAY_SIMULATION=/app/verifier/simulation.gd
USER 10001:10001
EXPOSE 8765
CMD ["python", "rendezvous.py", "--host", "0.0.0.0", "--max-per-ip", "128"]
