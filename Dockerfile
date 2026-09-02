# Ein Image, ein Prozess. Kein Node, kein Build-Schritt, keine zweite Sprache.
FROM python:3.12-slim

# tini fängt Signale ab, damit `docker compose down` sauber beendet statt zu killen.
RUN apt-get update && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Nicht als root laufen. /data gehört dem App-Nutzer, alles andere ist read-only
# (siehe read_only: true in docker-compose.yml).
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin runner \
    && mkdir -p /data && chown runner:runner /data
USER runner

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DB_PATH=/data/marathon.db

EXPOSE 8000
ENTRYPOINT ["/usr/bin/tini", "--"]
# --forwarded-allow-ips kommt aus der Umgebung (FORWARDED_ALLOW_IPS, in
# docker-compose.yml auf das Docker-Netz gesetzt). NICHT auf "*" stellen: dann
# glaubt uvicorn den vordersten X-Forwarded-For-Eintrag, und der stammt vom
# Client selbst — die Absender-IP wäre frei wählbar und die Login-Sperre wirkungslos.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
