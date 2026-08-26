# So pra rodar o titan_watcher.py sem precisar redigitar as credenciais toda
# vez que abrir um terminal novo - variaveis de ambiente definidas com $env:
# valem so pra sessao atual do terminal, entao isso ficava perdido a cada
# janela nova. Esse arquivo fica so na sua maquina - nao faz parte do que e
# enviado pro GoDeploy (so index.html e src/server.ts sao publicados).
# Garante que o script roda a partir da pasta onde ele proprio esta, mesmo
# que voce tenha aberto o terminal em outro lugar - titan_watcher.py precisa
# achar titan_bi_scraper.py (import local) no mesmo diretorio.
Set-Location $PSScriptRoot
. .\titan_credenciais.ps1  # define TITAN_EMAIL/TITAN_SENHA - arquivo fora do Git, ver titan_credenciais.ps1.example

python titan_watcher.py
