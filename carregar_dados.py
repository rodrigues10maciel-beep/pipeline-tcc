# -*- coding: utf-8 -*-
"""
Script de carga (Extract & Load) — lê o arquivo de coleta mais recente
(.xlsx ou .csv) da pasta de entrada e carrega os dados nas tabelas
dimensão e fato do Data Warehouse (PRODUTO, LOJA, OFERTA) no BigQuery.
Projetado para execução automatizada via Agendador de Tarefas do
Windows, sem necessidade de intervenção manual.

Pré-requisitos:
1. Google Cloud CLI instalado: cloud.google.com/sdk/docs/install
2. Autenticação via Application Default Credentials (ADC):
   gcloud auth application-default login
   gcloud config set project tcc-engenharia-computacao
3. Dependências: pip install google-cloud-bigquery pandas openpyxl
4. Ajustar PROJECT_ID e DATASET_ID conforme o ambiente
5. Execução: python carregar_dados.py

A autenticação utiliza as credenciais da conta Google do usuário,
dispensando chave de conta de serviço (JSON).

Funcionamento da pasta de entrada:
- O script localiza, na pasta onde está salvo, o arquivo .csv ou .xlsx
  mais recente (pela data de modificação).
- Após a carga, o arquivo é movido para a subpasta "processados" (com
  carimbo de data/hora), evitando reprocessamento em execuções futuras.
- Na ausência de arquivo novo, o script encerra sem erro — comportamento
  necessário para execução automatizada diária.
"""

import glob
import os
import shutil
from datetime import datetime

import pandas as pd
from google.cloud import bigquery
from google.cloud.exceptions import NotFound

# ---------------------------------------------------------------------
# CONFIGURAÇÃO
# ---------------------------------------------------------------------
PROJECT_ID = "tcc-engenharia-computacao"
DATASET_ID = "banco_dados_final"

# "." indica a mesma pasta onde este script está salvo.
PASTA_ENTRADA = "."
PASTA_PROCESSADOS = os.path.join(PASTA_ENTRADA, "processados")

# Padronização de nome de loja, aplicada na carga para evitar registros
# duplicados na tabela LOJA por variações de grafia (ex.: "kabum",
# "Kabum!").
MAPA_LOJAS = {
    "kabum": "KaBuM!",
    "kabum!": "KaBuM!",
    "pichau": "Pichau",
    "terabyte": "Terabyte",
    "amazon": "Amazon",
}


def padronizar_loja(nome):
    if nome is None or pd.isna(nome):
        return nome
    chave = str(nome).strip().lower()
    return MAPA_LOJAS.get(chave, str(nome).strip().title())


def get_client():
    return bigquery.Client(project=PROJECT_ID)


def garantir_tabelas(client):
    """Cria as tabelas PRODUTO, LOJA e OFERTA no dataset configurado,
    caso ainda não existam. O campo id_produto é definido como STRING
    por representar o identificador do produto no catálogo de cada
    loja, cujo formato varia entre lojas (numérico, código alfanumérico
    ou slug textual)."""
    dataset_ref = bigquery.DatasetReference(PROJECT_ID, DATASET_ID)

    tabela_produto = bigquery.Table(
        dataset_ref.table("PRODUTO"),
        schema=[
            bigquery.SchemaField("id_produto", "STRING"),
            bigquery.SchemaField("nome_produto", "STRING"),
            bigquery.SchemaField("categoria_produto", "STRING"),
            bigquery.SchemaField("marca", "STRING"),
        ],
    )

    tabela_loja = bigquery.Table(
        dataset_ref.table("LOJA"),
        schema=[
            bigquery.SchemaField("id_loja", "INT64"),
            bigquery.SchemaField("nome_loja", "STRING"),
            bigquery.SchemaField("url_base", "STRING"),
        ],
    )

    tabela_oferta = bigquery.Table(
        dataset_ref.table("OFERTA"),
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
    tabela_oferta.time_partitioning = bigquery.TimePartitioning(field="data_coleta")
    tabela_oferta.clustering_fields = ["id_produto", "id_loja"]

    for tabela in (tabela_produto, tabela_loja, tabela_oferta):
        try:
            client.get_table(tabela.reference)
        except NotFound:
            client.create_table(tabela)
            print(f"Tabela criada: {tabela.table_id}")


def encontrar_arquivo_mais_recente():
    """Localiza, na PASTA_ENTRADA, o arquivo .csv ou .xlsx mais recente
    pela data de modificação. Ignora a subpasta "processados". Retorna
    None se não houver arquivo novo."""
    candidatos = []
    for padrao in ("*.csv", "*.xlsx"):
        candidatos.extend(glob.glob(os.path.join(PASTA_ENTRADA, padrao)))

    if not candidatos:
        return None

    candidatos.sort(key=os.path.getmtime, reverse=True)
    return candidatos[0]


def mover_para_processados(caminho):
    """Move o arquivo já carregado para a subpasta 'processados', com
    carimbo de data/hora no nome, prevenindo reprocessamento."""
    os.makedirs(PASTA_PROCESSADOS, exist_ok=True)
    nome = os.path.basename(caminho)
    base, ext = os.path.splitext(nome)
    carimbo = datetime.now().strftime("%Y%m%d_%H%M%S")
    novo_nome = f"{base}_{carimbo}{ext}"
    destino = os.path.join(PASTA_PROCESSADOS, novo_nome)
    shutil.move(caminho, destino)
    print(f"Arquivo movido para: {destino}")


def _detectar_separador_csv(caminho):
    """Detecta automaticamente o separador do arquivo CSV (',' ou ';'),
    testando ambos e validando contra o conjunto de colunas esperado."""
    colunas_esperadas = {
        "id_produto_externo", "nome_produto", "categoria_produto",
        "preco", "nome_loja", "url_oferta", "data_coleta",
    }
    for sep in (",", ";"):
        try:
            amostra = pd.read_csv(caminho, sep=sep, nrows=5)
        except Exception:
            continue
        if colunas_esperadas.issubset(set(amostra.columns)):
            return sep
    return ","


def ler_arquivo_entrada(caminho):
    """Lê .xlsx ou .csv, detectando o formato pela extensão e, no caso
    de CSV, o separador utilizado."""
    extensao = os.path.splitext(caminho)[1].lower()

    if extensao == ".xlsx":
        df = pd.read_excel(caminho)
    elif extensao == ".csv":
        separador = _detectar_separador_csv(caminho)
        df = pd.read_csv(caminho, sep=separador)
    else:
        raise ValueError(
            f"Formato de arquivo não suportado: {extensao}. "
            "Use .xlsx ou .csv."
        )

    # id_produto_externo é tratado como texto para suportar os
    # diferentes formatos de identificador de catálogo entre lojas.
    df["id_produto_externo"] = df["id_produto_externo"].astype(str).str.strip()

    antes = len(df)

    # Remoção de duplicatas: múltiplas coletas do mesmo produto na
    # mesma loja em uma única execução de scraping são deduplicadas,
    # mantendo o registro mais antigo. A chave considera produto e
    # loja em conjunto, já que o identificador de catálogo não é
    # necessariamente único entre lojas diferentes.
    df["data_coleta"] = pd.to_datetime(df["data_coleta"])
    df = df.sort_values("data_coleta")
    df = df.drop_duplicates(subset=["id_produto_externo", "nome_loja"], keep="first")

    removidas = antes - len(df)
    if removidas > 0:
        print(f"Aviso: {removidas} linha(s) duplicada(s) removida(s) "
              f"(mantida a primeira coleta de cada uma).")

    return df.reset_index(drop=True)


def carregar_produto(client, df):
    """Insere produtos novos na tabela PRODUTO, evitando duplicação dos
    já existentes."""
    produtos = (
        df[["id_produto_externo", "nome_produto", "categoria_produto"]]
        .drop_duplicates()
        .rename(columns={"id_produto_externo": "id_produto"})
    )
    produtos["marca"] = None  # Preenchido na etapa de tratamento

    tabela = f"{PROJECT_ID}.{DATASET_ID}.PRODUTO"
    existentes = client.query(f"SELECT id_produto FROM `{tabela}`").to_dataframe()

    novos = produtos[~produtos["id_produto"].isin(existentes["id_produto"])]

    if not novos.empty:
        job = client.load_table_from_dataframe(novos, tabela)
        job.result()

    print(f"PRODUTO: {len(novos)} produto(s) novo(s) inserido(s) "
          f"({len(produtos) - len(novos)} já existiam).")


def carregar_loja(client, df):
    """Garante que cada loja (nome padronizado) possua um id_loja e
    retorna o mapa nome padronizado -> id."""
    tabela = f"{PROJECT_ID}.{DATASET_ID}.LOJA"
    existentes = client.query(f"SELECT id_loja, nome_loja FROM `{tabela}`").to_dataframe()

    mapa_lojas = dict(zip(existentes["nome_loja"], existentes["id_loja"]))
    proximo_id = int(existentes["id_loja"].max()) + 1 if not existentes.empty else 1

    nomes_padronizados = df["nome_loja"].apply(padronizar_loja).unique()

    novas_linhas = []
    for nome in nomes_padronizados:
        if nome not in mapa_lojas:
            mapa_lojas[nome] = proximo_id
            novas_linhas.append({"id_loja": proximo_id, "nome_loja": nome, "url_base": None})
            proximo_id += 1

    if novas_linhas:
        novas_df = pd.DataFrame(novas_linhas)
        job = client.load_table_from_dataframe(novas_df, tabela)
        job.result()

    print(f"LOJA: {len(novas_linhas)} loja(s) nova(s) inserida(s).")
    return mapa_lojas


def carregar_oferta(client, df, mapa_lojas):
    """Insere um registro de oferta para cada linha coletada."""
    tabela = f"{PROJECT_ID}.{DATASET_ID}.OFERTA"

    resultado = client.query(f"SELECT MAX(id_oferta) AS max_id FROM `{tabela}`").to_dataframe()
    max_atual = resultado["max_id"].iloc[0]
    proximo_id = int(max_atual) + 1 if pd.notna(max_atual) else 1

    ofertas = df.rename(columns={"id_produto_externo": "id_produto"}).copy()
    ofertas["id_oferta"] = range(proximo_id, proximo_id + len(ofertas))
    ofertas["nome_loja_padronizado"] = ofertas["nome_loja"].apply(padronizar_loja)
    ofertas["id_loja"] = ofertas["nome_loja_padronizado"].map(mapa_lojas)
    ofertas["data_coleta"] = pd.to_datetime(ofertas["data_coleta"])

    # Colunas preenchidas na etapa de tratamento (tratar_dados.py)
    ofertas["preco_medio_historico"] = pd.NA
    ofertas["outlier"] = False
    ofertas["situacao_compra"] = pd.NA

    colunas_finais = [
        "id_oferta", "id_produto", "id_loja", "preco",
        "preco_medio_historico", "outlier", "situacao_compra",
        "url_oferta", "data_coleta",
    ]
    ofertas = ofertas[colunas_finais]

    job = client.load_table_from_dataframe(ofertas, tabela)
    job.result()

    print(f"OFERTA: {len(ofertas)} oferta(s) inserida(s).")


def main():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Procurando arquivo novo em: "
          f"{os.path.abspath(PASTA_ENTRADA)}")

    caminho = encontrar_arquivo_mais_recente()
    if caminho is None:
        print("Nenhum arquivo .csv ou .xlsx novo encontrado na pasta. Nada a carregar.")
        return

    print(f"Arquivo encontrado: {caminho}")
    df = ler_arquivo_entrada(caminho)

    colunas_esperadas = {
        "id_produto_externo", "nome_produto", "categoria_produto",
        "preco", "nome_loja", "url_oferta", "data_coleta",
    }
    faltando = colunas_esperadas - set(df.columns)
    if faltando:
        raise ValueError(f"O arquivo está sem estas colunas esperadas: {faltando}")

    client = get_client()
    garantir_tabelas(client)

    carregar_produto(client, df)
    mapa_lojas = carregar_loja(client, df)
    carregar_oferta(client, df, mapa_lojas)

    mover_para_processados(caminho)

    print("\nCarga concluída com sucesso.")


if __name__ == "__main__":
    main()
