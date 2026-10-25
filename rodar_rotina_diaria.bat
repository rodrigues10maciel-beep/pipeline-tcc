@echo off
REM Alternativa pelo PC (Agendador de Tarefas, 07:00). Use esta OU o GitHub Actions, nunca os dois.
REM Ajuste as duas pastas abaixo.
cd /d "C:\Users\Usuario\Documents\BANCO DE DADOS TCC"
echo ===== %date% %time% ===== >> log_execucao.txt
python rotina_diaria.py --origem local --pasta "G:\Meu Drive\COLETAS" >> log_execucao.txt 2>&1
