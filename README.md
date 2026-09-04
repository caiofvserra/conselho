# QUEDA — Fiscalização de dados abertos do Crea-SP

Ferramenta de triagem que cruza as APIs públicas do Crea-SP e do PNCP em busca
de indícios de irregularidades e excessos: diárias/km/reembolsos, custo real de
comissionados e conselheiros, fracionamento de despesa, aditivos contratuais,
rede de influência entre órgãos do sistema Confea/Crea e fornecedores suspeitos.

> **Todo achado é indício, não prova.** Homonímias, falhas de cadastro e
> limitações das APIs geram falsos positivos. Confirme cada caso na fonte antes
> de qualquer denúncia ou publicação.

## Fontes

| Fonte | Uso |
|---|---|
| Portal Transparência (Implanta) `crea-sp.implanta.net.br` | empenhos, pagamentos, contratos, aditivos, licitações, convênios, quadro, conselheiros, comparativo de despesa |
| Dados Abertos Crea-SP (`/service-api`, host descoberto em runtime) | indenizações, quadro de colaboradores, mandatários |
| PNCP `pncp.gov.br` | contratos/atas/NF-e do Crea-SP e de órgãos relacionados (Confea, Mútua, outros CREAs) |
| BrasilAPI / minhareceita | ficha cadastral (CNPJ) dos fornecedores relevantes |

## Como rodar

```bash
pip install -r requirements.txt
python main.py faltando --desde auto --ate 2026  # o que falta coletar, sem rede
python main.py all --desde auto --ate 2026   # coleta + análise + relatório
python main.py selftest                      # valida o pipeline sem rede (fixtures)
```

- `--desde auto` sonda anos retroativamente até a API esvaziar (meta: alcançar
  exercícios pré-2016 para a série histórica de diárias e pessoal).
- Saída: `output/report.html` (dashboard autocontido) + `output/csv/*.csv`
  (evidência completa de cada achado — o HTML mostra uma amostra, o CSV traz
  tudo).

### Cada rodada fica guardada

Rodar de novo **não apaga o relatório anterior**. Os dados coletados são
reaproveitados; a saída é versionada:

```
output/
├── index.html              histórico de todas as rodadas
├── report.html             a mais recente, para abrir sem procurar
├── csv/                    evidência da mais recente
└── rodadas/
    ├── 2026-08-04_1930/    report.html + csv/ + achados.json
    └── 2026-08-05_1015/
```

O relatório novo abre com **o que mudou** desde a rodada anterior: achados que
apareceram, achados que sumiram e valores que se moveram. É a pergunta mais
útil depois da segunda execução, e ela só existe porque nada é sobrescrito —
inclusive quando a mudança vem de uma correção de bug, e não do órgão.

`QUEDA_RUN_ID` fixa o identificador da rodada, se você quiser reproduzir uma
execução específica.
- Cache de rede em `data/raw/` — reexecuções não repetem chamadas. Se a coleta
  parar (rate-limit, queda de sessão), rode de novo com `--retomar`: ele pula
  as etapas já concluídas e continua de onde parou.
- **Google Colab**: abra `notebooks/queda_colab.ipynb`
  ([link direto](https://colab.research.google.com/github/caiofvserra/queda/blob/claude/crea-sp-audit-tool-m9nw7t/notebooks/queda_colab.ipynb)) —
  recomendado, ver privacidade abaixo.

### Sobreviver à queda de sessão (Colab)

O Colab desconecta e apaga o disco. O clone do repositório é descartável, mas
`data/raw` guarda horas de coleta com throttle e `data/cache` guarda o que o
modelo local já leu — esses vão para o Google Drive:

```bash
export QUEDA_DATA_DIR=/content/drive/MyDrive/queda/data
export QUEDA_OUTPUT_DIR=/content/drive/MyDrive/queda/output
# OLLAMA_MODELS fica em disco LOCAL: o Drive é FUSE e o Ollama não sobe nele
python main.py all --desde auto --ate 2026 --retomar
```

A célula 1 do notebook faz isso. Reconectando, rode a mesma célula de coleta:
ela vai à rede **só pelos exercícios que ainda não vieram**. Etapa que veio
**vazia** é tentada de novo de propósito — dataset zerado quase sempre é
bloqueio por rajada (403), e 403 nunca é cacheado. O inventário da coleta é
gravado mesmo quando a rodada aborta no meio, então dá para ver o que já veio.

A retomada tem **dois níveis**. O fino é o checkpoint por **janela**: cada
exercício coletado vira um arquivo em `data/raw/checkpoints/<etapa>/`, e a
etapa só vai à rede pelas janelas que faltam. É a diferença entre "a sessão
caiu em 2019 e voltamos de 2019" e "caiu em 2019 e a etapa recomeça de 2026".
Como `data/raw` está no Drive, os checkpoints sobrevivem à desconexão. O
exercício ainda em curso tem checkpoint com validade curta (12h), porque ele
muda todo dia. Para ignorar checkpoints numa rodada, `QUEDA_CHECKPOINT=0`.

O grosso é o `--retomar`, que pula a etapa inteira — e ele **conta as janelas**
antes de pular. Uma etapa que trouxe 2016-2025 e falhou em 2013-2015 tem linhas
de sobra em `data/staged`; a versão anterior olhava só isso, dava a etapa por
pronta e o buraco não fechava em rodada nenhuma. Hoje ela é refeita, e refazer
é barato: as janelas que já estão em checkpoint saem do disco, sem rede.

`python main.py faltando` responde, sem gastar uma requisição, quantos
exercícios faltam em cada etapa e quais são — é o mesmo laço da coleta rodando
em seco, para que o inventário não possa divergir do que a coleta faria.

Para forçar recoleta de um período, `python main.py limpar --etapa licitacoes,atas`
apaga os marcadores de janela vazia dessas etapas (`--tudo` apaga também os
checkpoints com dado). Rodar **sem** `--retomar` não serve para isso: `--retomar`
só governa o pulo de etapa, e a leitura do checkpoint por janela é
incondicional — só `QUEDA_CHECKPOINT=0` a desliga.

Um efeito de contar janelas: passadas 12h, o exercício em curso volta a contar
como pendente (é o TTL do checkpoint do ano aberto, que muda todo dia) e as
etapas anuais são refeitas — uma requisição cada, com as demais janelas saindo
do disco. Rodar duas vezes dentro das 12h continua quase de graça; depois
delas, a rodada atualiza o exercício aberto em vez de não fazer nada.

**A análise sempre acontece.** Se o portal bloquear por ritmo no meio, `all`
não desiste da rodada: ele analisa o que já está em `data/staged`, entrega o
relatório e sai com código **2** para dizer que a coleta ficou incompleta. Antes
ele saía com 2 sem analisar nada, e quem operava do Colab descobria isso ao
abrir um zip de 164 bytes.

Toda requisição da rodada é registrada em `data/staged/_coleta_http.csv`:
host, caminho, status, tentativa, se veio do cache e quantos itens vieram. É
o que permite dizer **por que** um dataset veio vazio — 403 do WAF, 404 de
endpoint inexistente, ou 200 com lista vazia, que é o único caso em que a
ausência é do órgão e não nossa.

### Camada opcional de IA — modelo **local**, via Ollama

Nada do que o relatório afirma depende do modelo: todo número sai de regra
determinística. Ele entra só onde regra nenhuma serve — ler prosa e enquadrar
texto ambíguo.

O modelo roda **na sua máquina**. Nenhum dado do conselho — favorecido, valor,
deliberação — trafega para servidor de terceiro, o que é a mesma exigência que
rege as consultas às APIs do Crea. Sem chave, sem custo por token.

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama serve &
python main.py llm        # baixa o modelo e confere se está pronto
python main.py all        # ou `--sem-llm` para rodar sem a camada de IA
```

| Onde | O que o modelo faz | O que ele **não** faz |
|---|---|---|
| Resíduo da taxonomia | enquadra as contas que o regex não classificou, priorizadas por R$ | não recalcula valor; a origem fica registrada no achado de cobertura |
| Atas dos colegiados | lê a prosa da deliberação e extrai decisão, beneficiário e valor mencionado | não infere o que não está escrito, não julga legalidade |
| Resumo executivo | redige sobre os achados já calculados | não vê dado bruto nem produz número novo |

Modelos (variável `QUEDA_LLM_MODELO`):

| Modelo | Tamanho | Onde roda |
|---|---|---|
| `llama3.2:3b` | ~2,0 GB | CPU; qualidade menor |
| `qwen2.5:7b-instruct` | ~4,7 GB | **padrão** — GPU T4 do Colab gratuito |
| `gemma2:9b` | ~5,4 GB | GPU T4 |
| `qwen2.5:14b-instruct` | ~9 GB | GPU maior; melhor resultado |

Modelo pequeno erra mais que modelo grande, e o desenho conta com isso: o
esquema JSON é imposto pelo próprio Ollama, a temperatura é zero, os lotes são
curtos, a resposta só é aceita se cair numa categoria existente e itens de
baixa confiança são descartados. Tudo é cacheado em disco — rodadas seguintes
não repetem geração.

Outras variáveis: `QUEDA_OLLAMA_HOST` (padrão `http://localhost:11434`),
`QUEDA_LLM_LOTE`, `QUEDA_SEM_LLM=1`.

`networkx` (opcional, `pip install networkx`) dá a medida exata de
intermediação na rede — sem ele, a métrica cai para o grau de ligação.

### Quando a API barra: ingestão manual

Seis datasets voltam vazios da coleta automática — não porque o conselho não
os publique, mas porque o WAF barra o cliente. O portal serve os mesmos dados
a um navegador comum, e dá para baixá-los à mão.

Salve o arquivo como `<QUEDA_DATA_DIR>/manual/<dataset>.json` (ou `.csv`) e
rode de novo: a etapa usa o arquivo e **não vai à rede**. Nomes válidos:
`mandatarios`, `quadro_pessoal`, `atas`, `licitacoes`, `processos_itens`,
`indenizacoes_endpoint`. O JSON pode vir cru ou dentro do envelope do portal
(`{"result": true, "data": [...]}`) — as duas formas são aceitas.

A procedência fica registrada no diagnóstico da coleta: um número obtido à mão
não é reproduzível por quem só rodar o código, e o relatório precisa dizer isso.

Dado pessoal que venha nesses arquivos não sai do seu disco: `data/` é
gitignored, e o endereço de e-mail dos mandatários é descartado logo após a
extração do domínio — o relatório publica `unesp.br`, nunca o endereço.

## Frentes de análise

1. **Para onde vai o dinheiro** — taxonomia de dois eixos: a **natureza** (o
   que se comprou: salário, diária, aluguel, obra) vem da conta contábil e a
   **função** (para que unidade: fiscalização, atendimento, direção superior,
   órgãos políticos, administração) vem do centro de custo. A matriz cruzada
   separa "gasta muito com pessoal" de "gasta muito com pessoal que não
   fiscaliza", e isola as verbas indenizatórias pagas dentro das unidades que
   as autorizam.
2. **Superintendências e fiscalização** — o Crea fiscaliza 645 municípios, e a
   fiscalização acontece na rua. A frente divide o gasto em quatro camadas
   territoriais (direção central, administração central, superintendências
   regionais, unidades locais) e mede **quanto fica na estrutura e quanto chega
   à ponta**; decompõe o gasto de fiscalização em salário, verba e custeio
   operacional; flagra unidade territorial que quase nada gasta com a
   atividade-fim; e compara o ritmo de crescimento da despesa total com o da
   fiscalização. Quando o quadro de pessoal vem com lotação, mostra também onde
   estão as pessoas, não só o dinheiro.
3. **Representação e interesses** — quem ocupa o mandato e para onde vai
   o repasse institucional. Quando o rol de mandatários está disponível, mede
   **quantos são** — o denominador que falta para saber se a verba paga a
   conselheiros e inspetores é grande ou se a estrutura é grande —, isola os
   mandatos registrados sem data de início nem de fim (que atravessam qualquer
   filtro de período) e usa o **domínio do e-mail de contato** como único proxy
   público do vínculo de cada mandatário, cruzando-o com quem recebe dinheiro
   do conselho. Coincidência de domínio não é prova de nada: é ponto de partida
   para verificar impedimento e suspeição. As entidades de classe indicam o plenário e formam
   o colégio eleitoral que elege a diretoria; a frente classifica cada uma por
   interesse representado (patronal/empresarial, profissional, ensino, sistema
   Confea/Crea), mede o dinheiro que cada grupo recebe ano a ano, isola quem
   recebe por dois canais (convênio *e* contrato) e cruza **assento × dinheiro**.
   Mede o vínculo financeiro entre o conselho e sua base política — não prova
   que um conselheiro votou a favor de quem o indicou, o que exigiria a ata
   nominal da votação.
4. **Orçamento** — orçado → empenhado → liquidado → pago por exercício; contas
   que estouram a dotação ano após ano (subestimação na peça orçamentária, não
   imprevisto); diárias executadas acima do aprovado; e quanto de cada real
   arrecadado do profissional voltou como fiscalização.
5. **Opacidade** — o que se esconde atrás de rótulos agregadores tipo "DIVERSOS
   FUNCIONÁRIOS", decomposto pela conta contábil que vem na própria linha do
   pagamento.
6. **Diárias e verbas a pessoas** — classificadas **só** pela conta contábil
   (nunca pelo histórico ou pelo nome do favorecido, que era a origem dos
   falsos positivos). Série anual com CAGR real, ranking por exercício,
   beneficiários persistentes, volume anual implausível, mais de 12 verbas num
   mês, acumulação de verbas no mesmo dia, e viagem comprada de PJ isolada como
   custeio.
7. **Pessoal** — salário nominal do quadro + "renda invisível" (indenizações
   por pessoa); série histórica por conta contábil.
8. **Casos concentrados** — entidades associativas que levam o nome do próprio
   conselho, obra/nova sede, contratos de valor excepcional, pessoa física
   prestando serviço.
9. **Empenhos e pagamentos** — concentração por fornecedor (HHI sobre o mercado
   real, sem folha nem repasse legal), fracionamento aferido contra o limite de
   dispensa **vigente em cada exercício**, anatomia das anulações (empenho
   fantasma, corrida de dezembro, reempenho no ano seguinte), pago > empenhado.
10. **Contratos e licitações** — aditivos de valor acima do limite legal
   (separados dos de prazo), pagamento após vigência, contratação direta,
   cross-check Implanta × PNCP, e a **razão pago ÷ contratado por fornecedor**:
   pagar muito acima do instrumento sugere execução sem cobertura contratual;
   contrato de vigência encerrada com quase nada pago sugere valor inflado na
   origem. Contrato é compromisso e pagamento é caixa — a frente diz isso em
   cada achado, e a junção é por nome normalizado, não por CNPJ, porque o
   portal nem sempre publica o CNPJ da contratada.
11. **Rede de influência** — fornecedores compartilhados com Confea/Mútua/outros
   CREAs, sócios homônimos de conselheiros e funcionários, **grupos econômicos
   de fato** (componente conexa fornecedor–sócio: empresas ligadas por uma
   pessoa não são concorrentes), nós de maior intermediação, convênios com
   teste de ciclo eleitoral e detecção de rateio por valor redondo idêntico.
12. **Deliberações dos colegiados** — decisões com efeito financeiro extraídas
   das atas pelo modelo local e cruzadas com os pagamentos.
13. **Forense estatística** — Benford de 1 e 2 dígitos (limiares de Nigrini
    próprios de cada teste), represamento abaixo do limite de dispensa do ano,
    valores redondos, pares favorecido+valor repetidos, sazonalidade de
    dezembro, estornos.
14. **Fornecedores** — situação cadastral não-ativa na Receita ponderada pela
    distância até o último pagamento, empresa criada às vésperas do primeiro
    pagamento, concentração geográfica dos fornecedores contra o alcance
    estadual do conselho, quanto do valor chega a micro e pequena empresa, e
    CNAE declarado sem relação com o objeto contratado. Toda métrica publica a
    **cobertura** (que fatia do valor tem ficha cadastral consultada) — sem
    isso o percentual engana.
15. **Receita e cobertura** — de onde vem o dinheiro (anuidade compulsória,
    ART, multa, dívida ativa) e, principalmente, a receita de **ART como
    denominador**: cada ART é uma obra ou serviço técnico registrado, então o
    gasto de fiscalização dividido por ela mede **cobertura**, não esforço
    absoluto. A multa de fiscalização tem leitura dupla e explícita — é produto
    da fiscalização e é incentivo perverso.
16. **Sazonalidade** — as doze colunas `VALOR_JAN..VALOR_DEZ` da despesa por
    centro de custo são o único eixo mensal que a coleta entrega: concentração
    em dezembro por unidade e por conta (Lei 4.320/1964, arts. 35 e 38), verba
    a pessoas paga em lote em vez de distribuída ao longo do ano, unidade com
    perfil mensal atípico, e divergência entre a soma dos meses e o total do
    exercício.
17. **Plano de contas oficial** — a despesa relida pela hierarquia que o
    próprio conselho declara, em vez da nossa taxonomia por regex: cobertura da
    taxonomia contra a estrutura oficial (uma família inteira caindo em "não
    classificado" é buraco nosso e aparece nomeada), contas desenhadas que
    nunca receberam lançamento, e execução sob rótulo sem correspondência no
    plano.
18. **Empenho prévio (Lei 4.320/1964, art. 60)** — pagamento sem empenho
    correspondente nos dados publicados. Quando a chave de ligação entre as duas
    bases simplesmente não é publicada, a frente troca a lista de acusações por
    **um** achado de metodologia: 100% "sem empenho" não são milhares de
    ilegalidades, é campo vazio.

    A chave tem duas armadilhas, e as duas já morderam. A primeira é o rótulo:
    o portal publica o número do empenho como `NUMERO` no conjunto Empenhos e
    como `NUMERO_EMPENHO` no conjunto Pagamentos — procurar o segundo nos dois
    lados faz o cruzamento inteiro cair no par credor + valor e publicar
    `casados_por_numero: 0`, um zero que parece resultado e é ausência de
    tentativa. A segunda é a numeração, que **reinicia a cada exercício**: o
    número sozinho não identifica empenho nenhum, e casar por ele absolve
    pagamento sem empenho por coincidência de sequência. Por isso o casamento
    por número passa por uma **conferência pelo dinheiro** — o `VALOR_PAGO` que
    o dataset de empenhos declara para cada número tem de bater com a soma dos
    pagamentos que apontam para ele. Reprovado na conferência, o casamento é
    descartado e o caso vira diagnóstico (`empenhos-chave-nao-casa`), nunca
    acusação. `_regressao_chave_empenho`, no selftest, tranca as duas.
19. **Diagnóstico da coleta** — a ferramenta auditando a si mesma: o que veio,
    o que faltou, que exercícios ficaram fora, **qual análise está desligada
    por falta de qual fonte** e, lido do registro de status HTTP, **por que**
    cada fonte vazia veio vazia — separando "o órgão não publica" (achado de
    transparência, Lei 12.527/2011, art. 8º) de "não conseguimos coletar"
    (limitação nossa). Poucos achados numa frente cuja fonte veio vazia não é
    sinal de regularidade.

Thresholds (teto de diária, limites de dispensa por ano, jetons por mês,
janelas, anos eleitorais) são parâmetros em `src/queda/config.py`.

## Privacidade do operador

- Somente GETs anônimos a APIs públicas: sem login, token, cookie ou qualquer
  identificador; User-Agent genérico de navegador; ~1 requisição/segundo.
- A camada de IA é **local** (Ollama): os dados coletados não são enviados a
  nenhum serviço de terceiro para serem lidos ou classificados.
- Rodando no Colab, o tráfego parte de IPs do Google — não da sua conexão.
- `data/` e `output/` são **gitignored**: nada de dados ou relatórios vai para
  o GitHub. Se anonimato importa, mantenha este repositório **privado** — a
  conta dona do repositório é o único vínculo público possível.
- A API de CNPJ é consultada apenas para fornecedores pessoa jurídica.

## Limitações conhecidas

- Cruzamentos por **nome normalizado** (as APIs mascaram CPF): homonímia gera
  falso positivo — confirmar por CPF/documentos antes de concluir.
- A profundidade histórica depende do que o portal expõe; se a sondagem não
  alcançar pré-2016, a alternativa manual são os relatórios de gestão anuais e
  as prestações de contas junto ao TCU.
- PNCP só cobre contratações a partir de 2021 (Lei 14.133).
- Processos judiciais (DataJud/CNJ) ficam fora do escopo automático atual.

### Parâmetros que precisam de conferência antes de virar prova

A ferramenta publica sempre qual parâmetro usou, justamente para que estes
pontos possam ser conferidos. Todos vivem em `src/queda/config.py`:

- **Limite de dispensa por exercício** (`LIMITES_DISPENSA`) — os valores até
  2023 estão fixados; os exercícios seguintes são atualizados por decreto anual
  e herdam o último valor conhecido até serem confirmados.
- **Teto de jetons por mês** (`MAX_JETONS_MES`) — muda por resolução do Confea;
  confira a vigente no exercício analisado.
- **Anos de eleição do Crea** (`ANOS_ELEITORAIS_CREA`) — o teste de ciclo
  eleitoral dos convênios é sensível a esta lista.
- **CNPJ do órgão-alvo** (`ORGAO_ALVO_CNPJ_CANDIDATOS`) — as fontes públicas
  divergem entre dois números, então nenhum é aceito no escuro: cada candidato
  é conferido no PNCP (razão social + UF) e, se nenhum passar, o bloco PNCP vem
  vazio em vez de vir com os contratos de outro conselho.
