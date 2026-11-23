@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if not exist .venv (
  echo [1/3] 가상환경 생성
  python -m venv .venv
  if errorlevel 1 (
    py -3 -m venv .venv
    if errorlevel 1 (
      echo 가상환경 생성 실패: Python 3.11+ 설치 및 PATH 를 확인하세요.
      pause
      exit /b 1
    )
  )
)

call .venv\Scripts\activate.bat

echo [2/3] 의존성 설치
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements.txt
if errorlevel 1 (
  echo 의존성 설치 실패
  pause
  exit /b 1
)

if not exist .env (
  echo [3/3] .env 생성 ^(.env.example 복사^)
  copy /y .env.example .env >nul
  echo.
  echo !! .env 를 열어 DISCORD_TOKEN, OWNER_IDS, PUBLIC_BASE_URL 을 채운 뒤 다시 실행하세요.
  notepad .env
  pause
  exit /b 0
)

echo [3/3] 설정 점검
python run.py --check
if errorlevel 1 (
  echo.
  echo !! 위 항목을 .env 에 채운 뒤 다시 실행하세요.
  pause
  exit /b 1
)

python run.py
pause
