FROM python:3.13-alpine

RUN apk add --no-cache gcc musl-dev libffi-dev libxml2-dev libxslt-dev

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN adduser -D -u 1000 appuser
USER 1000:1000

EXPOSE 8000

CMD ["python", "main.py", "--http"]
