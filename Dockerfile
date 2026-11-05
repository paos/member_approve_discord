FROM python:3.14-slim
WORKDIR /app
COPY requirements-lock.txt .
RUN pip install --no-cache-dir -r requirements-lock.txt && useradd --uid 10001 --create-home app
COPY mensa ./mensa
RUN mkdir /app/data && chown app:app /app/data
USER app
EXPOSE 8766
CMD ["python", "-m", "uvicorn", "mensa.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8766", "--no-access-log"]
