#!/bin/bash
# NOVA Checker API — Start Script
CHECKER_THREADS=${CHECKER_THREADS:-200} \
CHECKER_RETRIES=${CHECKER_RETRIES:-1} \
CHECKER_CAPTCHA_RETRIES=${CHECKER_CAPTCHA_RETRIES:-1} \
PREFER_US=${PREFER_US:-true} \
python3 -u -m uvicorn api_server:app \
  --host 0.0.0.0 \
  --port ${PORT:-8000} \
  --workers 1 \
  --timeout-keep-alive 60 \
  --log-level warning
