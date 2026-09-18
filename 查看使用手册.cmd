@echo off
if not exist "%~dp0output\pdf\Ashare_User_Guide.html" (
  echo User guide not found. Please keep the output folder with this project.
  pause
  exit /b 1
)
start "" "%~dp0output\pdf\Ashare_User_Guide.html"
