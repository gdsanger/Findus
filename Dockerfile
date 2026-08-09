FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# OCR toolchain for the extraction cascade (apps.documents.extraction,
# #1009): tesseract for the OCR stage, poppler for rendering PDF pages to
# images (OCR + vision stages both need pixels, not the PDF's text layer).
#
# Bewusst NICHT dazugekommen: ein Office-Konverter (LibreOffice headless)
# für die Brief-Ausgabe (#1095). Word und PDF werden beide direkt aus
# Python gerendert (python-docx bzw. fpdf2, siehe
# apps.documents.letter_render) -- ein ~400 MB schweres Office-Paket samt
# Subprozess-Handling wäre die einzige Alternative gewesen und hätte hier
# als Systemabhängigkeit stehen müssen.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-deu \
        tesseract-ocr-eng \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

EXPOSE 8000 8001
