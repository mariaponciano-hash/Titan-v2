-- ============================================================================
-- Seed de referencia: COMO PREENCHER enderecos_para_ticket corretamente.
--
-- Serve de contrato pra quem alimenta a tabela (o bot). Os nomes das colunas
-- end_* mapeiam 1:1 no objeto `variables.correct_address` que o cx-ticketcreator
-- espera, e o mapeamento NAO e intuitivo:
--
--   end_logradouro   -> address1   rua / avenida
--   end_numero       -> address2   O NUMERO  (nao e "linha 2 do endereco")
--   end_complemento  -> address3   apto / bloco / fundos
--   end_bairro       -> address4   O BAIRRO  (nao e "linha 4")
--   end_cidade       -> city
--   end_uf           -> state      sigla, 2 letras
--   end_cep          -> zipcode    com ou sem mascara
--   end_pais         -> country_code
--   novo_endereco    -> fullAddress (string composta)
--
-- POR QUE AS PARTES SAO OBRIGATORIAS: o texto que a transportadora recebe sai do
-- fullAddress verbatim, mas a Regra 1 do creator (o que pode ser alterado depois
-- do envio) compara SO as partes - city, state, address1, address2, zipcode - e
-- nunca faz parse da string. Linha com fullAddress e sem partes passa do gate de
-- ausencia e morre como ADDRESS_UNVERIFIABLE. Minimo pra Regra 1 decidir:
-- cidade + UF, mais logradouro ou CEP.
--
-- SEGURANCA DESTE SEED: os `pedido` abaixo sao ids INEXISTENTES de proposito.
-- Se alguem configurar o TICKET_WEBHOOK_SECRET real com estas linhas na tabela,
-- a criacao para no passo 4 (busca do pedido na origem) e NAO abre ticket em
-- transportadora nenhuma. Dado de teste nao pode ser capaz de mandar pedido de
-- mudanca de endereco pra um cliente de verdade que nunca pediu nada.
--
-- Rodar DEPOIS do 005. Substitui o seed antigo (002).
-- ============================================================================

-- limpa o seed anterior (que usava NF como pedido e nao tinha as partes)
delete from public.enderecos_para_ticket
 where pedido in ('1259695', '10002669', '968609');

insert into public.enderecos_para_ticket (
  pedido, marca, status,
  endereco_atual, novo_endereco,
  end_logradouro, end_numero, end_complemento, end_bairro,
  end_cidade, end_uf, end_cep, end_pais
) values

-- ---------------------------------------------------------------------------
-- (1) CASO COMPLETO, COM COMPLEMENTO. E o formato ideal: todas as partes
--     preenchidas, e o fullAddress composto na MESMA ordem que o creator usaria
--     (address1, address2, address3, address4, city, state, country_code, zipcode).
-- ---------------------------------------------------------------------------
('TESTE-KOK-0001', 'KOKESHI', 'aguardando',
 'Rua das Laranjeiras, 88, Centro, Campinas, SP, BR, 13010-100',
 'Avenida Brigadeiro Faria Lima, 3477, Conjunto 142, Itaim Bibi, Sao Paulo, SP, BR, 04538-133',
 'Avenida Brigadeiro Faria Lima', '3477', 'Conjunto 142', 'Itaim Bibi',
 'Sao Paulo', 'SP', '04538-133', 'BR'),

-- ---------------------------------------------------------------------------
-- (2) SEM COMPLEMENTO. Casa, sem apto/bloco. end_complemento fica NULL - nao
--     inventar "N/A", "-", "sem complemento": qualquer texto ali vai virar
--     address3 e entrar no endereco que a transportadora le.
-- ---------------------------------------------------------------------------
('TESTE-RIT-0002', 'RITUARIA', 'aguardando',
 'Rua Coronel Vicente, 401, Cidade Baixa, Porto Alegre, RS, BR, 90030-040',
 'Rua Padre Chagas, 415, Moinhos de Vento, Porto Alegre, RS, BR, 90570-080',
 'Rua Padre Chagas', '415', null, 'Moinhos de Vento',
 'Porto Alegre', 'RS', '90570-080', 'BR'),

-- ---------------------------------------------------------------------------
-- (3) SO NUMERO E COMPLEMENTO MUDARAM, mesma rua e mesma cidade. E o unico tipo
--     de alteracao que a J&T aceita depois do envio (Regra 1). Serve pra testar
--     o caminho que DEVE passar mesmo na transportadora mais restritiva.
-- ---------------------------------------------------------------------------
('TESTE-LES-0003', 'LESCENT', 'aguardando',
 'Rua Sete de Setembro, 120, Centro, Curitiba, PR, BR, 80020-120',
 'Rua Sete de Setembro, 388, Apto 71, Centro, Curitiba, PR, BR, 80020-120',
 'Rua Sete de Setembro', '388', 'Apto 71', 'Centro',
 'Curitiba', 'PR', '80020-120', 'BR'),

-- ---------------------------------------------------------------------------
-- (4) MUDANCA DE CIDADE. Deve ser recusada pela Regra 1 com
--     ADDRESS_CHANGE_BLOCKED / blocked_reason = different_city. Nao e bug: e o
--     desfecho correto, e existe pra a tela mostrar `bloqueado` de verdade em
--     vez de a gente so acreditar que mostraria.
-- ---------------------------------------------------------------------------
('TESTE-BAR-0004', 'BARBOURS', 'aguardando',
 'Rua Barao de Itapetininga, 255, Republica, Sao Paulo, SP, BR, 01042-001',
 'Rua Rio de Janeiro, 1001, Sala 5, Lourdes, Belo Horizonte, MG, BR, 30160-041',
 'Rua Rio de Janeiro', '1001', 'Sala 5', 'Lourdes',
 'Belo Horizonte', 'MG', '30160-041', 'BR'),

-- ---------------------------------------------------------------------------
-- (5) CONTRAEXEMPLO - E ASSIM QUE **NAO** SE PREENCHE.
--     Tem fullAddress e nao tem nenhuma parte. Passa do gate de ausencia
--     (ADDRESS_MISSING) e morre na Regra 1 como ADDRESS_UNVERIFIABLE, porque a
--     regra nao tem cidade/UF pra comparar. Esta aqui de proposito, pra a fila
--     mostrar esse desfecho e ninguem achar que "mandei o endereco" e o bastante.
-- ---------------------------------------------------------------------------
('TESTE-AUA-0005', 'AUA', 'aguardando',
 'Rua Antonio Basilio, 500, Tijuca, Rio de Janeiro, RJ, BR, 20511-190',
 'entregar na casa da minha mae, rua ali perto do mercado, numero 45, Niteroi',
 null, null, null, null,
 null, null, null, 'BR'),

-- ---------------------------------------------------------------------------
-- (6) e (7) MESMO `pedido` EM MARCAS DIFERENTES. Sao pedidos distintos: cada
--     origem tem sua propria sequencia de id, entao o mesmo numero pode existir
--     em duas marcas. E o teste de regressao da chave unica (pedido, marca) -
--     com unique so em `pedido`, a segunda linha seria recusada e a cliente
--     ficaria sem ticket sem ninguem ver.
-- ---------------------------------------------------------------------------
('TESTE-DUP-0006', 'BYSAMIA', 'aguardando',
 'Rua Dona Ana Neri, 77, Sao Cristovao, Rio de Janeiro, RJ, BR, 20950-100',
 'Estrada do Galeao, 2200, Bloco 3 Apto 904, Jardim Guanabara, Rio de Janeiro, RJ, BR, 21931-002',
 'Estrada do Galeao', '2200', 'Bloco 3 Apto 904', 'Jardim Guanabara',
 'Rio de Janeiro', 'RJ', '21931-002', 'BR'),

('TESTE-DUP-0006', 'APICE', 'aguardando',
 'Avenida Conselheiro Aguiar, 1748, Boa Viagem, Recife, PE, BR, 51111-020',
 'Rua Ernesto de Paula Santos, 187, Apto 1502, Boa Viagem, Recife, PE, BR, 51021-330',
 'Rua Ernesto de Paula Santos', '187', 'Apto 1502', 'Boa Viagem',
 'Recife', 'PE', '51021-330', 'BR')

on conflict (pedido, marca) do update set
  endereco_atual  = excluded.endereco_atual,
  novo_endereco   = excluded.novo_endereco,
  end_logradouro  = excluded.end_logradouro,
  end_numero      = excluded.end_numero,
  end_complemento = excluded.end_complemento,
  end_bairro      = excluded.end_bairro,
  end_cidade      = excluded.end_cidade,
  end_uf          = excluded.end_uf,
  end_cep         = excluded.end_cep,
  end_pais        = excluded.end_pais,
  status          = 'aguardando',
  tentativas      = 0,
  ultimo_erro     = null,
  ultimo_erro_em  = null,
  ultimo_code     = null,
  blocked_reason  = null,
  ticket_id       = null,
  ticket_status   = null,
  ticket_reference = null,
  proxima_tentativa_em = null,
  disparado_em    = null;


-- ---------------------------------------------------------------------------
-- CONFERENCIA: mostra, pra cada linha, o objeto correct_address EXATO que o
-- worker vai montar. Espelha montarPayloadCriacaoEndereco() do server.ts - se
-- os dois divergirem algum dia, e aqui que aparece.
--
-- Repare na linha TESTE-AUA-0005: correct_address vem com tudo vazio menos o
-- fullAddress. E esse o formato que morre em ADDRESS_UNVERIFIABLE.
-- ---------------------------------------------------------------------------
select
  pedido,
  marca,
  -- as 9 brands do creator sao exatamente a marca em minusculo (BYSAMIA ->
  -- bysamia, sem underscore - diferente do datamart, que usa by_samia)
  lower(marca) as brand_enviado,
  jsonb_pretty(jsonb_build_object(
    'address1',     coalesce(end_logradouro, ''),
    'address2',     coalesce(end_numero, ''),
    'address3',     coalesce(end_complemento, ''),
    'address4',     coalesce(end_bairro, ''),
    'city',         coalesce(end_cidade, ''),
    'state',        coalesce(end_uf, ''),
    'zipcode',      coalesce(end_cep, ''),
    'country_code', coalesce(end_pais, 'BR'),
    'fullAddress',  coalesce(novo_endereco, '')
  )) as correct_address,
  -- Regra 1 consegue decidir? Precisa de cidade + UF, mais logradouro ou CEP.
  case
    when end_cidade is not null and end_uf is not null
     and (end_logradouro is not null or end_cep is not null)
    then 'ok'
    else 'INSUFICIENTE -> ADDRESS_UNVERIFIABLE'
  end as regra1_consegue_avaliar
from public.enderecos_para_ticket
order by marca, pedido;
