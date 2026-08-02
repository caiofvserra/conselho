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
- Cache de rede em `data/raw/` — reexecuções não repetem chamadas.
- **Google Colab**: abra `notebooks/queda_colab.ipynb` (recomendado — ver
  privacidade abaixo).

## Frentes de análise

1. **Diárias e indenizações** — extraídas dos *pagamentos* (filtro textual de
   conta/histórico), já que os endpoints dedicados costumam vir vazios — o que,
   por si só, vira achado de transparência. Série anual por categoria de
   beneficiário (conselheiro/comissionado/efetivo), top beneficiários,
   volume anual implausível, concentração, PJ recebendo indenização.
2. **Custo real por categoria** — salário nominal do quadro + "renda invisível"
   (indenizações por pessoa); série histórica por conta contábil; razão
   indenizações de comissionados+conselheiros ÷ folha de efetivos.
3. **Empenhos e pagamentos** — concentração por fornecedor (HHI), padrão de
   fracionamento de despesa, anulações em massa, pago > empenhado.
4. **Contratos e licitações** — aditivos acima do limite legal, pagamento após
   vigência, contratação direta, cross-check Implanta × PNCP.
5. **Rede de influência** — fornecedores compartilhados entre Crea-SP e
   Confea/Mútua/outros CREAs, sócios (QSA) homônimos de conselheiros e
   funcionários, sócios em comum entre "concorrentes", convênios recorrentes,
   repasses a entidades do sistema. Grafo interativo no dashboard.
6. **Fornecedores** — situação cadastral não-ativa na Receita, empresa criada
   às vésperas do primeiro pagamento.

Thresholds (teto de diária, limite de dispensa, janelas, etc.) são parâmetros
em `src/queda/config.py`.

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
