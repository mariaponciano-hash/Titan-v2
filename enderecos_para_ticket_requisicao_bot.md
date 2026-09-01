# Como o bot escreve em `enderecos_para_ticket`

Uma linha por pedido com troca de endereço pedida pelo cliente. A Central cuida
do resto: ela é quem decide quando o pedido saiu, quem chama o
`cx-ticketcreator`, e quem registra o desfecho.

O formato campo por campo está em `enderecos_para_ticket_formato_bot.xlsx`. Este
arquivo é só a requisição.

---

## O endpoint

```
POST https://ozwcyrkzsqzmavjtsmsp.supabase.co/rest/v1/enderecos_para_ticket?on_conflict=pedido,marca
```

| header | valor |
| --- | --- |
| `apikey` | a **service_role** key do projeto |
| `Authorization` | `Bearer <service_role key>` |
| `Content-Type` | `application/json` |
| `Prefer` | `resolution=merge-duplicates,return=representation` |

### Por que service_role e não a anon

A tabela guarda endereço de cliente, então tem RLS habilitado **sem nenhuma
policy para a role `anon`** — a chave anon está no código-fonte de um app
público, e liberar `anon` aqui equivaleria a publicar endereço de cliente na
internet.

Com a chave anon o insert é recusado assim (verificado):

```json
{ "code": "42501", "message": "new row violates row-level security policy for table \"enderecos_para_ticket\"" }
```

Se preferirem não dar service_role ao bot, o caminho é criar uma policy de
**INSERT apenas** amarrada a uma role dedicada e emitir um JWT dessa role. Dá
mais trabalho de setup e é mais seguro; a service_role funciona hoje.

---

## O corpo

```json
{
  "pedido": "2824057",
  "marca": "KOKESHI",
  "endereco_atual": "Rua das Laranjeiras, 88, Centro, Campinas, SP, BR, 13010-100",
  "novo_endereco": "Avenida Faria Lima, 3477, Conjunto 142, Itaim Bibi, Sao Paulo, SP, BR, 04538-133",
  "end_logradouro": "Avenida Faria Lima",
  "end_numero": "3477",
  "end_complemento": "Conjunto 142",
  "end_bairro": "Itaim Bibi",
  "end_cidade": "Sao Paulo",
  "end_uf": "SP",
  "end_cep": "04538-133",
  "end_pais": "BR"
}
```

Aceita array para inserir várias de uma vez.

### Três campos que não são o que o nome sugere

- **`pedido`** é o `orderId` da **origem** da marca — id do Cosmos nas 7 marcas,
  número Shopify na Ápice. **Não é a NF** (que é o que a Central usa nas telas
  antigas) e **não é a `reference`** — essa o `cx-ticketcreator` devolve na
  criação, e a Central guarda em `ticket_reference`.
- **`end_numero`** vira `address2` no payload do creator, que é o **número**.
- **`end_bairro`** vira `address4`, que é o **bairro**.

A nomenclatura `address1..4` é herdada dos payloads do n8n e não segue lógica
nenhuma. Trocar `end_numero` por complemento manda o número errado para a
transportadora, e isso **não dá erro em lugar nenhum**.

### Endereço estruturado não é opcional

A regra do creator que decide se a alteração é permitida compara **só as partes**
(`city`, `state`, `address1`, `address2`, `zipcode`) e nunca faz parse do
`novo_endereco`. Linha que tem apenas o texto livre passa da checagem de "veio
endereço?" e morre depois como `ADDRESS_UNVERIFIABLE`.

Mínimo para a regra conseguir decidir: **cidade + UF, mais logradouro ou CEP.**

---

## `status`: nunca mandar

Essa é a regra que evita o pior bug possível aqui.

O `on_conflict=pedido,marca` com `resolution=merge-duplicates` atualiza **apenas
as colunas presentes no corpo**. Omitindo `status`, um segundo pedido de troca no
mesmo pedido atualiza o endereço e **deixa o estado da Central intacto**.

Se o bot mandar `"status": "aguardando"` numa linha que já está `criado`, a
Central vai entender que é caso novo e **abrir um segundo ticket** na
transportadora para o mesmo pedido.

O default do banco já é `aguardando` no insert novo, então não há motivo para
mandar.

> **Consequência conhecida:** com `status` omitido, um segundo endereço num pedido
> que já tem ticket aberto fica gravado na tabela mas **não chega à
> transportadora** — o creator responde `TICKET_EXISTS`. É melhor que abrir ticket
> duplicado, mas é um caminho sem saída, e o tratamento dele é decisão de
> processo ainda em aberto.

---

## Também não mandar

Estas são da Central e escrever nelas atropela o controle de fila:

```
status · tentativas · ultimo_erro · ultimo_erro_em · ultimo_code
blocked_reason · proxima_tentativa_em · ticket_reference · ticket_id
ticket_status · ticket_deadline · delivery_status · delivery_date
tracking_code · carrier · cliente · status_visto_em · disparado_em
criado_em · atualizado_em
```

---

## Exemplo completo

```bash
curl -s -X POST \
  "https://ozwcyrkzsqzmavjtsmsp.supabase.co/rest/v1/enderecos_para_ticket?on_conflict=pedido,marca" \
  -H "apikey: $SB_SERVICE_KEY" \
  -H "Authorization: Bearer $SB_SERVICE_KEY" \
  -H "Content-Type: application/json" \
  -H "Prefer: resolution=merge-duplicates,return=representation" \
  -d '{
    "pedido": "2824057",
    "marca": "KOKESHI",
    "novo_endereco": "Avenida Faria Lima, 3477, Conjunto 142, Itaim Bibi, Sao Paulo, SP, BR, 04538-133",
    "end_logradouro": "Avenida Faria Lima",
    "end_numero": "3477",
    "end_complemento": "Conjunto 142",
    "end_bairro": "Itaim Bibi",
    "end_cidade": "Sao Paulo",
    "end_uf": "SP",
    "end_cep": "04538-133"
  }'
```

---

## Respostas

| situação | resposta |
| --- | --- |
| inseriu | `201` com a linha (por causa do `return=representation`) |
| já existia, atualizou | `200` com a linha |
| chave errada / anon | `401` + `code 42501` (RLS) |
| coluna que não existe no corpo | `400` + `code 42703` com o nome da coluna |
| `status` com valor fora da lista | `400` + `code 23514` (o CHECK da tabela) |

Não há validação de endereço no banco: linha com o endereço incompleto **entra
normalmente** e só falha depois, quando a Central chama o creator e recebe
`ADDRESS_UNVERIFIABLE`. Aparece na aba "Tickets a Disparar" como
`precisa_humano`, com o code na linha.

---

## Marcas aceitas

Em maiúsculo, como a Central grava (ela converte para minúsculo ao chamar o
creator):

`KOKESHI` · `APICE` · `RITUARIA` · `BARBOURS` · `LESCENT` · `AUA` · `BYSAMIA` ·
`GOCASE` · `YENZAH`

Marca fora dessa lista entra na tabela mas nunca dispara: a Central não consegue
mapear para nenhuma das 9 `brand` que o creator aceita, e a linha para em `erro`
com `MARCA_DESCONHECIDA`.
