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
  parar com aviso de rate-limit, rode o mesmo comando de novo: ele retoma de
  onde parou.
- **Google Colab**: abra `notebooks/queda_colab.ipynb`
  ([link direto](https://colab.research.google.com/github/caiofvserra/queda/blob/claude/crea-sp-audit-tool-m9nw7t/notebooks/queda_colab.ipynb)) —
  recomendado, ver privacidade abaixo.

### Camada opcional de IA

Nada do que o relatório afirma depende de LLM: todo número sai de regra
determinística. O modelo entra só onde regra nenhuma serve.

```bash
pip install anthropic
export QUEDA_ANTHROPIC_API_KEY=sk-ant-...
python main.py all            # ou `--sem-llm` para desligar mesmo com a chave
```

| Onde | O que o LLM faz | O que ele **não** faz |
|---|---|---|
| Resíduo da taxonomia | enquadra as contas que o regex não classificou, priorizadas por R$ | não recalcula valor; a origem fica registrada no achado de cobertura |
| Atas dos colegiados | lê a prosa da deliberação e extrai decisão, beneficiário e valor mencionado | não infere o que não está escrito, não julga legalidade |
| Resumo executivo | redige sobre os achados já calculados | não vê dado bruto nem produz número novo |

Todas as respostas são cacheadas em disco; rodadas seguintes não repetem
chamadas. `networkx` (opcional) dá a medida exata de intermediação na rede —
sem ele, a métrica cai para o grau de ligação.

## Frentes de análise

1. **Para onde vai o dinheiro** — taxonomia de dois eixos: a **natureza** (o
   que se comprou: salário, diária, aluguel, obra) vem da conta contábil e a
   **função** (para que unidade: fiscalização, atendimento, direção superior,
   órgãos políticos, administração) vem do centro de custo. A matriz cruzada
   separa "gasta muito com pessoal" de "gasta muito com pessoal que não
   fiscaliza", e isola as verbas indenizatórias pagas dentro das unidades que
   as autorizam.
2. **Orçamento** — orçado → empenhado → liquidado → pago por exercício; contas
   que estouram a dotação ano após ano (subestimação na peça orçamentária, não
   imprevisto); diárias executadas acima do aprovado; e quanto de cada real
   arrecadado do profissional voltou como fiscalização.
3. **Opacidade** — o que se esconde atrás de rótulos agregadores tipo "DIVERSOS
   FUNCIONÁRIOS", decomposto pela conta contábil que vem na própria linha do
   pagamento.
4. **Diárias e verbas a pessoas** — classificadas **só** pela conta contábil
   (nunca pelo histórico ou pelo nome do favorecido, que era a origem dos
   falsos positivos). Série anual com CAGR real, ranking por exercício,
   beneficiários persistentes, volume anual implausível, mais de 12 verbas num
   mês, acumulação de verbas no mesmo dia, e viagem comprada de PJ isolada como
   custeio.
5. **Pessoal** — salário nominal do quadro + "renda invisível" (indenizações
   por pessoa); série histórica por conta contábil.
6. **Casos concentrados** — entidades associativas que levam o nome do próprio
   conselho, obra/nova sede, contratos de valor excepcional, pessoa física
   prestando serviço.
7. **Empenhos e pagamentos** — concentração por fornecedor (HHI sobre o mercado
   real, sem folha nem repasse legal), fracionamento aferido contra o limite de
   dispensa **vigente em cada exercício**, anatomia das anulações (empenho
   fantasma, corrida de dezembro, reempenho no ano seguinte), pago > empenhado.
8. **Contratos e licitações** — aditivos de valor acima do limite legal
   (separados dos de prazo), pagamento após vigência, contratação direta,
   cross-check Implanta × PNCP.
9. **Rede de influência** — fornecedores compartilhados com Confea/Mútua/outros
   CREAs, sócios homônimos de conselheiros e funcionários, **grupos econômicos
   de fato** (componente conexa fornecedor–sócio: empresas ligadas por uma
   pessoa não são concorrentes), nós de maior intermediação, convênios com
   teste de ciclo eleitoral e detecção de rateio por valor redondo idêntico.
10. **Deliberações dos colegiados** — decisões com efeito financeiro extraídas
    das atas por LLM e cruzadas com os pagamentos.
11. **Forense estatística** — Benford de 1 e 2 dígitos (limiares de Nigrini
    próprios de cada teste), represamento abaixo do limite de dispensa do ano,
    valores redondos, pares favorecido+valor repetidos, sazonalidade de
    dezembro, estornos.
12. **Fornecedores** — situação cadastral não-ativa na Receita ponderada pela
    distância até o último pagamento, empresa criada às vésperas do primeiro
    pagamento.
13. **Diagnóstico da coleta** — a ferramenta auditando a si mesma: o que veio,
    o que faltou, que exercícios ficaram fora e **qual análise está desligada
    por falta de qual fonte**. Poucos achados numa frente cuja fonte veio vazia
    não é sinal de regularidade.

Thresholds (teto de diária, limites de dispensa por ano, jetons por mês,
janelas, anos eleitorais) são parâmetros em `src/queda/config.py`.

## Privacidade do operador

- Somente GETs anônimos a APIs públicas: sem login, token, cookie ou qualquer
  identificador; User-Agent genérico de navegador; ~1 requisição/segundo.
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
