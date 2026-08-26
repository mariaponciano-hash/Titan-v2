# Roda o titan_backfill.py sem abrir janela (--headless) pra uma janela
# deslizante dos ultimos 10 dias até hoje (25/08/2026, a pedido da Ivna -
# ampliado de 5 pra 10: um pedido preso num status nao-final some da janela
# de atualizacao depois desses N dias e fica travado pra sempre, ver
# conversa) - pega tanto pedidos novos quanto atualizacoes de status/romaneio
# dos ultimos dias, sem precisar reprocessar o historico inteiro. Volume
# real (~7.800 pedidos/dia em media, medido no backfill historico de 3
# meses) deixa 10 dias bem abaixo do teto de exportacao do Power BI
# (~150.000 linhas) - sem risco de truncamento. Pensado pra ser chamado pela
# Tarefa Agendada do Windows "TitanBackfillDiario" (ver instrucoes no fim do
# arquivo), nao pra rodar manualmente com frequencia - mas pode rodar na mao
# pra testar.
#
# So fica nesta maquina - nunca faz parte do que sobe pro GoDeploy (so
# index.html e src/server.ts sao publicados por la).
Set-Location $PSScriptRoot
. .\titan_credenciais.ps1  # define TITAN_EMAIL/TITAN_SENHA - arquivo fora do Git, ver titan_credenciais.ps1.example

$dataFinal = Get-Date -Format 'dd/MM/yyyy'
$dataInicial = (Get-Date).AddDays(-10).ToString('dd/MM/yyyy')

$logFile = Join-Path $PSScriptRoot 'titan_backfill_diario.log'
$timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Add-Content -Path $logFile -Value "`n=== $timestamp - rodando $dataInicial a $dataFinal ==="

python titan_backfill.py --data-inicial $dataInicial --data-final $dataFinal --headless *>> $logFile

Add-Content -Path $logFile -Value "=== fim (codigo $LASTEXITCODE) ==="

# --- Como isso fica agendado -----------------------------------------------
# Tarefa "TitanBackfillDiario" no Agendador de Tarefas do Windows, criada via:
#   schtasks /create /tn "TitanBackfillDiario" /sc daily /st 06:00 /f /tr "powershell.exe -ExecutionPolicy Bypass -File \"C:\Users\Notebook\Desktop\central_tickets\rodar_titan_backfill_diario.ps1\""
# Roda todo dia as 06:00, mas so se o seu usuario estiver logado no Windows
# naquele horario (nao configurada para rodar com a tela travada/desligada -
# isso exigiria guardar a senha do SEU LOGIN do Windows, o que e um risco
# maior e nao foi feito). Pra ver o resultado de cada execucao, confira
# titan_backfill_diario.log nesta mesma pasta.
