FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# OCR toolchain for the extraction cascade (apps.documents.extraction,
# #1009): tesseract for the OCR stage, poppler for rendering PDF pages to
# images (OCR + vision stages both need pixels, not the PDF's text layer).
# wkhtmltopdf renders the cleaned mail body to a PDF (apps.ingest.mail_body,
# #1070) -- a binary shelled out to, same pattern as tesseract/poppler.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-deu \
        tesseract-ocr-eng \
        poppler-utils \
        wkhtmltopdf \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

EXPOSE 8000 8001
