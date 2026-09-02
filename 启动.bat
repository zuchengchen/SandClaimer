@echo off
cd /d "%~dp0"
title Sand �ʸ���ȡ��

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  echo [����] û���ҵ� Python�����Ȱ�װ Python 3��
  echo        ��װʱ�ǵù�ѡ "Add python.exe to PATH"��
  echo        ���ص�ַ�� https://www.python.org/downloads/
  echo.
  pause
  exit /b 1
)

%PY% -c "import webview, requests, websocket" 1>nul 2>nul
if errorlevel 1 (
  echo [�״�����] ���ڰ�װ��������Ҫ���������Ժ򡭡�
  %PY% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo [����] ������װʧ�ܣ�������������ԡ�
    pause
    exit /b 1
  )
)

echo �������� Sand �ʸ���ȡ������
%PY% app.py

echo.
echo �������˳������û�������ڻ��б���������������Ϣ��ͼ�������ߡ�
pause
