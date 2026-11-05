-- =====================================================================
-- Pipeline Analítico para Recomendação de Compras
-- TCC · Engenharia da Computação · USJT
--
-- Criação das tabelas do Data Warehouse no BigQuery (modelo estrela).
--
-- Este script é equivalente ao que o pipeline (carregar_dados.py,
-- função garantir_tabelas) cria automaticamente na primeira carga, e
-- serve como documentação formal da estrutura. Pode ser executado no
-- console do BigQuery antes da primeira carga.
--
-- Antes de rodar: substitua `tcc-engenharia-computacao.banco_dados_final`
-- pelo seu projeto e dataset, e crie o dataset caso ainda não exista.
--
-- Observações:
-- 1. O BigQuery não impõe chaves primárias e estrangeiras. As relações
--    abaixo estão descritas nos comentários e nas descrições dos campos,
--    e a integridade é mantida pelos scripts de carga em Python.
-- 2. Ordem sugerida: PRODUTO -> LOJA -> OFERTA.
-- =====================================================================


-- ---------------------------------------------------------------------
-- PRODUTO (dimensão) — dados cadastrais de cada produto.
-- Chave primária (lógica): id_produto
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `tcc-engenharia-computacao.banco_dados_final.PRODUTO` (
  id_produto         STRING OPTIONS(description="Identificador do produto no catálogo da loja (numérico, alfanumérico ou texto). Chave primária lógica."),
  nome_produto       STRING OPTIONS(description="Nome comercial do produto"),
  categoria_produto  STRING OPTIONS(description="Categoria do produto (ex.: Placas de vídeo, Teclados)"),
  marca              STRING OPTIONS(description="Fabricante identificada pelo nome do produto; vazio se não reconhecida")
);


-- ---------------------------------------------------------------------
-- LOJA (dimensão) — nome padronizado de cada loja.
-- Chave primária (lógica): id_loja
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `tcc-engenharia-computacao.banco_dados_final.LOJA` (
  id_loja    INT64  OPTIONS(description="Identificador numérico da loja. Chave primária lógica."),
  nome_loja  STRING OPTIONS(description="Nome padronizado da loja (ex.: KaBuM!, Amazon, Pichau, Terabyte)"),
  url_base   STRING OPTIONS(description="Endereço principal da loja (reservado; ainda não preenchido)")
);


-- ---------------------------------------------------------------------
-- OFERTA (fato) — uma linha por produto, por loja, por coleta.
-- Chave primária (lógica): id_oferta
-- Chaves estrangeiras (lógicas): id_produto -> PRODUTO.id_produto
--                                id_loja    -> LOJA.id_loja
-- Particionada por dia de coleta e clusterizada por produto e loja,
-- que são os filtros mais usados no dashboard.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `tcc-engenharia-computacao.banco_dados_final.OFERTA` (
  id_oferta              INT64     OPTIONS(description="Identificador único da oferta. Chave primária lógica."),
  id_produto             STRING    OPTIONS(description="Produto da oferta. Referencia PRODUTO.id_produto."),
  id_loja                INT64     OPTIONS(description="Loja da oferta. Referencia LOJA.id_loja."),
  preco                  FLOAT64   OPTIONS(description="Preço em reais no momento da coleta"),
  preco_medio_historico  FLOAT64   OPTIONS(description="Média dos preços anteriores do produto, sem outliers; vazio na primeira coleta"),
  outlier                BOOL      OPTIONS(description="TRUE se o preço varia mais de 30% em relação à média histórica"),
  situacao_compra        STRING    OPTIONS(description="Boa compra, Preço médio, Evitar ou Sem histórico"),
  url_oferta             STRING    OPTIONS(description="Link direto para a oferta na loja"),
  data_coleta            TIMESTAMP OPTIONS(description="Data e hora da coleta. Campo de particionamento.")
)
PARTITION BY TIMESTAMP_TRUNC(data_coleta, DAY)
CLUSTER BY id_produto, id_loja;


-- ---------------------------------------------------------------------
-- CONTROLE_CARGA (apoio) — não precisa ser criada manualmente.
-- A rotina diária (rotina_diaria.py) cria esta tabela sozinha e registra
-- nela cada arquivo processado, para nunca carregar o mesmo arquivo duas
-- vezes. Os campos estão descritos em docs/banco_de_dados.md.
-- ---------------------------------------------------------------------
