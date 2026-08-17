@echo off
echo [1/3] Gerekli paketler kuruluyor...
pip install -r requirements.txt

echo [2/3] .exe derleniyor (bu birkac dakika surebilir)...
pyinstaller --noconfirm --onefile --windowed --name "RLStatsTracker" tracker_app.py

echo [3/3] Tamamlandi!
echo Cikti dosyan: dist\RLStatsTracker.exe
pause
