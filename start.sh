#!/usr/bin/env bash
# VENDRIX - Linux/macOS 실행 스크립트
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 가 없습니다. Python 3.11+ 를 설치하세요."
  exit 1
fi

if [ ! -d .venv ]; then
  echo "[1/3] 가상환경 생성"
  python3 -m venv .venv || {
    echo "가상환경 생성 실패: python3-venv 패키지를 설치하세요. (예: sudo apt install python3-venv)"
    exit 1
  }
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "[2/3] 의존성 설치"
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt

if [ ! -f .env ]; then
  echo "[3/3] .env 생성 (.env.example 복사)"
  cp .env.example .env
  echo
  echo "!! .env 를 열어 DISCORD_TOKEN, OWNER_IDS, PUBLIC_BASE_URL 을 채운 뒤 다시 실행하세요."
  echo "   nano .env"
  exit 0
fi

echo "[3/3] 설정 점검"
if ! python run.py --check; then
  echo
  echo "!! 위 항목을 .env 에 채운 뒤 다시 실행하세요."
  exit 1
fi

exec python run.py
