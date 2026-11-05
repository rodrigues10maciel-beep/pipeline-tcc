-- =====================================================================
-- Consultas de exemplo — conferência da carga e análises básicas
-- Rode uma de cada vez no console do BigQuery.
-- =====================================================================


-- 1) Arquivos processados e o que aconteceu com cada um
SELECT arquivo_nome, status, linhas_lidas, ofertas_inseridas, detalhe, processado_em
FROM `tcc-engenharia-computacao.banco_dados_final.CONTROLE_CARGA`
ORDER BY processado_em;


-- 2) Quantidade de ofertas por dia (procure por datas faltando)
SELECT DATE(data_coleta) AS dia, COUNT(*) AS ofertas
FROM `tcc-engenharia-computacao.banco_dados_final.OFERTA`
GROUP BY dia
ORDER BY dia;


-- 3) Ofertas sem classificação (esperado: 0 depois do tratamento)
SELECT COUNT(*) AS sem_situacao
FROM `tcc-engenharia-computacao.banco_dados_final.OFERTA`
WHERE situacao_compra IS NULL;


-- 4) Ofertas por loja
SELECT l.nome_loja, COUNT(*) AS ofertas, COUNT(DISTINCT o.id_produto) AS produtos
FROM `tcc-engenharia-computacao.banco_dados_final.OFERTA` AS o
JOIN `tcc-engenharia-computacao.banco_dados_final.LOJA`   AS l USING (id_loja)
GROUP BY l.nome_loja
ORDER BY ofertas DESC;


-- 5) Distribuição da situação de compra por loja
SELECT l.nome_loja, o.situacao_compra, COUNT(*) AS ofertas
FROM `tcc-engenharia-computacao.banco_dados_final.OFERTA` AS o
JOIN `tcc-engenharia-computacao.banco_dados_final.LOJA`   AS l USING (id_loja)
GROUP BY l.nome_loja, o.situacao_compra
ORDER BY l.nome_loja, ofertas DESC;


-- 6) Melhores oportunidades da coleta mais recente
--    (maiores descontos em relação à média histórica, sem outliers)
SELECT
  p.nome_produto,
  p.categoria_produto,
  l.nome_loja,
  o.preco,
  ROUND(o.preco_medio_historico, 2)                          AS preco_medio_historico,
  ROUND((1 - o.preco / o.preco_medio_historico) * 100, 1)    AS desconto_percentual,
  o.url_oferta
FROM `tcc-engenharia-computacao.banco_dados_final.OFERTA`  AS o
JOIN `tcc-engenharia-computacao.banco_dados_final.PRODUTO` AS p USING (id_produto)
JOIN `tcc-engenharia-computacao.banco_dados_final.LOJA`    AS l USING (id_loja)
WHERE DATE(o.data_coleta) = (
        SELECT MAX(DATE(data_coleta))
        FROM `tcc-engenharia-computacao.banco_dados_final.OFERTA`
      )
  AND o.situacao_compra = 'Boa compra'
  AND NOT o.outlier
ORDER BY desconto_percentual DESC
LIMIT 20;


-- 7) Histórico de preço de um produto (troque o identificador)
SELECT DATE(o.data_coleta) AS dia, l.nome_loja, o.preco, o.situacao_compra
FROM `tcc-engenharia-computacao.banco_dados_final.OFERTA` AS o
JOIN `tcc-engenharia-computacao.banco_dados_final.LOJA`   AS l USING (id_loja)
WHERE o.id_produto = '727508'
ORDER BY o.data_coleta;
