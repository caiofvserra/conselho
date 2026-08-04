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
python main.py all --desde auto --ate 2026   # coleta + análise + relatório
python main.py selftest                      # valida o pipeline sem rede (fixtures)
```

- `--desde auto` sonda anos retroativamente até a API esvaziar (meta: alcançar
  exercícios pré-2016 para a série histórica de diárias e pessoal).
- Saída: `output/report.html` (dashboard autocontido) + `output/csv/*.csv`
  (evidência bruta de cada achado).
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
`--retomar` pula cada etapa que já tem dado em disco. Etapa que veio **vazia**
é tentada de novo de propósito — dataset zerado quase sempre é bloqueio por
rajada (403), e 403 nunca é cacheado. O inventário da coleta é gravado mesmo
quando a rodada aborta no meio, então dá para ver o que já veio.

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
3. **Representação e interesses** — quem indica os conselheiros e para onde vai
   o repasse institucional. As entidades de classe indicam o plenário e formam
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
   cross-check Implanta × PNCP.
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
    pagamento.
15. **Diagnóstico da coleta** — a ferramenta auditando a si mesma: o que veio,
    o que faltou, que exercícios ficaram fora e **qual análise está desligada
    por falta de qual fonte**. Poucos achados numa frente cuja fonte veio vazia
    não é sinal de regularidade.

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
