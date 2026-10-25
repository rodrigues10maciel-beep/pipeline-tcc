# -*- coding: utf-8 -*-
"""
Script de tratamento (Transform) — recalcula atributos derivados das
tabelas do Data Warehouse: identificação de marca e padronização de
loja (dimensões PRODUTO e LOJA), além de média histórica, detecção de
outlier e classificação de situação de compra para cada oferta (fato
OFERTA).

O BigQuery em modo Sandbox não permite operações UPDATE/MERGE. Por
esse motivo, o script lê a tabela por completo, recalcula os valores
em pandas e regrava a tabela inteira (WRITE_TRUNCATE).

O cálculo da média histórica e da classificação de cada oferta utiliza
exclusivamente coletas anteriores à oferta em análise, nunca coletas
futuras, prevenindo viés de antecipação (look-ahead bias) na série. A
primeira coleta de cada produto não possui histórico anterior e recebe
a classificação "Sem histórico".

Pré-requisitos:
1. gcloud auth application-default login
   gcloud config set project <projeto>
2. python -m pip install google-cloud-bigquery pandas numpy db-dtypes
3. Ajustar PROJECT_ID e DATASET_ID conforme o ambiente
4. Execução: python tratar_dados.py

O script é idempotente: pode ser executado quantas vezes forem
necessárias, pois recalcula os valores a partir dos dados atuais das
tabelas em cada execução, sem acumular erro entre execuções.
"""

import pandas as pd
from google.cloud import bigquery

# ---------------------------------------------------------------------
# CONFIGURAÇÃO
# ---------------------------------------------------------------------
PROJECT_ID = "tcc-engenharia-computacao"
DATASET_ID = "banco_dados_final"

TABELA_PRODUTO = f"{PROJECT_ID}.{DATASET_ID}.PRODUTO"
TABELA_LOJA = f"{PROJECT_ID}.{DATASET_ID}.LOJA"
TABELA_OFERTA = f"{PROJECT_ID}.{DATASET_ID}.OFERTA"

# Margem de classificação: variação de 10% em torno da média histórica
# define as faixas de "Boa compra" e "Evitar".
MARGEM_SITUACAO = 0.10

# Limite de variação (em relação à média histórica) para que uma
# oferta seja classificada como outlier.
LIMITE_OUTLIER = 0.30

COLUNAS_OFERTA = [
    "id_oferta", "id_produto", "id_loja", "preco",
    "preco_medio_historico", "outlier", "situacao_compra",
    "url_oferta", "data_coleta",
]


def get_client():
    return bigquery.Client(project=PROJECT_ID)


# ---------------------------------------------------------------------
# PADRONIZAÇÃO DE LOJA E IDENTIFICAÇÃO DE MARCA
# ---------------------------------------------------------------------
MAPA_LOJAS = {
    "kabum": "KaBuM!",
    "kabum!": "KaBuM!",
    "pichau": "Pichau",
    "terabyte": "Terabyte",
    "amazon": "Amazon",
}

MAPA_MARCAS = {
    "redragon": "Redragon",
    "asus": "ASUS",
    "gigabyte": "Gigabyte",
    "msi": "MSI",
    "nvidia": "NVIDIA",
    "amd": "AMD",
    "intel": "Intel",
    "corsair": "Corsair",
    "kingston": "Kingston",
    "galax": "Galax",
}


def padronizar_loja(nome):
    if nome is None or pd.isna(nome):
        return nome
    chave = str(nome).strip().lower()
    return MAPA_LOJAS.get(chave, str(nome).strip().title())


def identificar_marca(nome_produto):
    if nome_produto is None or pd.isna(nome_produto):
        return None
    nome = str(nome_produto).lower()
    for palavra, marca in MAPA_MARCAS.items():
        if palavra in nome:
            return marca
    return None


def atualizar_produto_marca(client):
    """Recalcula a marca de cada produto a partir do nome e regrava a
    tabela PRODUTO por completo."""
    df = client.query(f"SELECT * FROM `{TABELA_PRODUTO}`").to_dataframe()
    if df.empty:
        return

    df["marca"] = df["nome_produto"].apply(identificar_marca)

    job_config = bigquery.LoadJobConfig(write_disposition="WRITE_TRUNCATE")
    job = client.load_table_from_dataframe(df, TABELA_PRODUTO, job_config=job_config)
    job.result()

    qtd_identificadas = df["marca"].notna().sum()
    print(f"PRODUTO: marca identificada em {qtd_identificadas} de {len(df)} produto(s).")


def atualizar_loja_padronizada(client):
    """Padroniza o nome de cada loja e regrava a tabela LOJA por
    completo."""
    df = client.query(f"SELECT * FROM `{TABELA_LOJA}`").to_dataframe()
    if df.empty:
        return

    df["nome_loja"] = df["nome_loja"].apply(padronizar_loja)

    job_config = bigquery.LoadJobConfig(write_disposition="WRITE_TRUNCATE")
    job = client.load_table_from_dataframe(df, TABELA_LOJA, job_config=job_config)
    job.result()

    print(f"LOJA: {len(df)} nome(s) de loja padronizado(s).")


# ---------------------------------------------------------------------
# OUTLIER E SITUAÇÃO DE COMPRA
# ---------------------------------------------------------------------
def verificar_outlier(preco, media):
    if media is None or pd.isna(media) or media == 0:
        return False
    variacao = abs(preco - media) / media
    return bool(variacao > LIMITE_OUTLIER)


def classificar_compra(preco, media):
    if media is None or pd.isna(media) or media == 0:
        return "Sem histórico"
    percentual = preco / media
    if percentual <= 1 - MARGEM_SITUACAO:
        return "Boa compra"
    elif percentual <= 1 + MARGEM_SITUACAO:
        return "Preço médio"
    else:
        return "Evitar"


def calcular_tratamento(df: pd.DataFrame) -> pd.DataFrame:
    """Para cada oferta, considera exclusivamente as coletas anteriores
    do mesmo produto (nunca coletas futuras) para calcular outlier,
    média histórica e situação de compra."""
    df = df.sort_values(["id_produto", "data_coleta"]).reset_index(drop=True)

    df["preco_medio_historico"] = pd.NA
    df["outlier"] = False
    df["situacao_compra"] = "Sem histórico"

    for id_produto, grupo in df.groupby("id_produto", sort=False):
        grupo = grupo.sort_values("data_coleta")
        precos_anteriores = []  # Preços já observados, excluindo outliers

        for idx, row in grupo.iterrows():
            preco_atual = row["preco"]

            if not precos_anteriores:
                df.at[idx, "preco_medio_historico"] = pd.NA
                df.at[idx, "outlier"] = False
                df.at[idx, "situacao_compra"] = "Sem histórico"
            else:
                media = sum(precos_anteriores) / len(precos_anteriores)

                is_outlier = verificar_outlier(preco_atual, media)
                situacao = classificar_compra(preco_atual, media)

                df.at[idx, "preco_medio_historico"] = media
                df.at[idx, "outlier"] = is_outlier
                df.at[idx, "situacao_compra"] = situacao

            # Um preço só integra o histórico de comparações futuras
            # quando não é um outlier, evitando distorcer a média.
            if not df.at[idx, "outlier"]:
                precos_anteriores.append(preco_atual)

    return df


def main():
    client = get_client()

    print("Atualizando marca dos produtos...")
    atualizar_produto_marca(client)

    print("Padronizando nomes de loja...")
    atualizar_loja_padronizada(client)

    print("\nLendo ofertas do BigQuery...")
    df = client.query(f"SELECT * FROM `{TABELA_OFERTA}`").to_dataframe()

    if df.empty:
        print("Nenhuma oferta encontrada na tabela. Nada a tratar.")
        return

    print(f"{len(df)} oferta(s) encontradas, de {df['id_produto'].nunique()} produto(s) distintos.")

    resultado = calcular_tratamento(df)
    resultado = resultado[COLUNAS_OFERTA]

    print("Regravando a tabela OFERTA com os resultados calculados...")
    job_config = bigquery.LoadJobConfig(
        write_disposition="WRITE_TRUNCATE",
        schema=[
            bigquery.SchemaField("id_oferta", "INT64"),
            bigquery.SchemaField("id_produto", "STRING"),
            bigquery.SchemaField("id_loja", "INT64"),
            bigquery.SchemaField("preco", "FLOAT64"),
            bigquery.SchemaField("preco_medio_historico", "FLOAT64"),
            bigquery.SchemaField("outlier", "BOOL"),
            bigquery.SchemaField("situacao_compra", "STRING"),
            bigquery.SchemaField("url_oferta", "STRING"),
            bigquery.SchemaField("data_coleta", "TIMESTAMP"),
        ],
    )
    job = client.load_table_from_dataframe(resultado, TABELA_OFERTA, job_config=job_config)
    job.result()

    resumo = resultado["situacao_compra"].value_counts()
    qtd_outliers = int(resultado["outlier"].sum())

    print("\nTratamento concluído com sucesso.")
    print(f"Outliers identificados: {qtd_outliers}")
    print("Situação de compra:")
    for situacao, qtd in resumo.items():
        print(f"  {situacao}: {qtd}")


if __name__ == "__main__":
    main()
