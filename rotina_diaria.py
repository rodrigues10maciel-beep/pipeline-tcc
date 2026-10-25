# -*- coding: utf-8 -*-
"""
Rotina diária de ingestão — Google Drive (ou pasta local) -> BigQuery.

Automatiza, em uma única execução, o que antes era feito manualmente
pelo prompt de comando (carregar_dados.py seguido de tratar_dados.py):

1. Lista os arquivos de coleta (.csv, .xlsx e .zip) da origem
   configurada: pasta do Google Drive (via API, adequada à execução na
   nuvem) ou pasta local (por exemplo, a pasta sincronizada pelo Google
   Drive para computador).
2. Descarta os arquivos já processados, consultando a tabela de
   controle CONTROLE_CARGA no BigQuery. Na primeira execução a tabela
   está vazia, de modo que todos os arquivos existentes na pasta são
   lidos (carga retroativa).
3. Para cada arquivo novo, em ordem cronológica: extrai os .zip, valida
   as colunas, carrega PRODUTO, LOJA e OFERTA reutilizando as funções de
   carregar_dados.py e registra o arquivo na tabela de controle.
4. Ao final, executa tratar_dados.main(), que recalcula média histórica,
   outliers e situação de compra. O tratamento também é executado quando
   existem ofertas ainda sem classificação (por exemplo, após uma
   execução anterior interrompida).

Idempotência — a rotina pode ser executada quantas vezes for necessário
sem duplicar dados:
- Nível de arquivo: um arquivo só é reprocessado se o seu conteúdo (MD5)
  for alterado.
- Nível de linha: antes de inserir em OFERTA, são descartadas as ofertas
  cuja chave natural (produto, loja, data_coleta) já existe na tabela.
  Isso evita duplicar, por exemplo, arquivos carregados manualmente
  antes da automação.
- Arquivos que abrangem mais de um dia de coleta são divididos e
  carregados dia a dia, pois a deduplicação de carregar_dados.py mantém
  uma única coleta por produto/loja em cada arquivo.

Uso:
  python rotina_diaria.py --origem drive --pasta-id <ID_DA_PASTA>
  python rotina_diaria.py --origem local --pasta "G:\\Meu Drive\\cargas"
  python rotina_diaria.py --simular        (apenas lista; nada é gravado)

Código de saída: 0 em caso de sucesso; 1 se algum arquivo falhou (o
agendador, como o GitHub Actions, sinaliza a falha).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import sys
import tempfile
import traceback
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Set, Tuple

import pandas as pd
from google.cloud import bigquery
from google.cloud.exceptions import NotFound

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import carregar_dados as cd  # noqa: E402
import tratar_dados as td  # noqa: E402

# ---------------------------------------------------------------------
# CONFIGURAÇÃO
# ---------------------------------------------------------------------
PROJECT_ID = cd.PROJECT_ID
DATASET_ID = cd.DATASET_ID
TABELA_OFERTA = f"{PROJECT_ID}.{DATASET_ID}.OFERTA"
TABELA_CONTROLE = f"{PROJECT_ID}.{DATASET_ID}.CONTROLE_CARGA"

EXTENSOES_DADOS = (".csv", ".xlsx")
EXTENSOES_ORIGEM = EXTENSOES_DADOS + (".zip",)

COLUNAS_ESPERADAS = {
    "id_produto_externo", "nome_produto", "categoria_produto",
    "preco", "nome_loja", "url_oferta", "data_coleta",
}

SCOPES_DRIVE = ["https://www.googleapis.com/auth/drive.readonly"]
MIME_PASTA_DRIVE = "application/vnd.google-apps.folder"

SCHEMA_CONTROLE = [
    bigquery.SchemaField("arquivo_id", "STRING"),
    bigquery.SchemaField("arquivo_nome", "STRING"),
    bigquery.SchemaField("arquivo_md5", "STRING"),
    bigquery.SchemaField("arquivo_tamanho", "INT64"),
    bigquery.SchemaField("arquivo_modificado_em", "TIMESTAMP"),
    bigquery.SchemaField("origem", "STRING"),
    bigquery.SchemaField("status", "STRING"),
    bigquery.SchemaField("linhas_lidas", "INT64"),
    bigquery.SchemaField("ofertas_inseridas", "INT64"),
    bigquery.SchemaField("detalhe", "STRING"),
    bigquery.SchemaField("processado_em", "TIMESTAMP"),
]


def log(mensagem: str = "") -> None:
    if mensagem:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {mensagem}", flush=True)
    else:
        print(flush=True)


def formatar_tamanho(n_bytes: int) -> str:
    valor = float(n_bytes)
    for unidade in ("B", "KB", "MB", "GB"):
        if valor < 1024 or unidade == "GB":
            return f"{valor:.0f} {unidade}" if unidade == "B" else f"{valor:.1f} {unidade}"
        valor /= 1024
    return f"{n_bytes} B"


# ---------------------------------------------------------------------
# ORIGEM DOS ARQUIVOS
# ---------------------------------------------------------------------
@dataclass
class ArquivoOrigem:
    """Arquivo localizado na origem (Drive ou pasta local). O conteúdo só
    é lido quando ler_bytes() é chamado, evitando downloads de arquivos
    já processados."""
    id: str
    nome: str
    md5: str
    tamanho: int
    modificado_em: datetime  # UTC, com fuso
    ler_bytes: Callable[[], bytes]

    @property
    def chave(self) -> Tuple[str, str]:
        return (self.id, self.md5)


def _nome_ignoravel(nome: str) -> bool:
    return nome.startswith(("~$", "."))


# --- Pasta local -----------------------------------------------------
def _md5_arquivo(caminho: str) -> str:
    h = hashlib.md5()
    with open(caminho, "rb") as f:
        for bloco in iter(lambda: f.read(1024 * 1024), b""):
            h.update(bloco)
    return h.hexdigest()


def _ler_arquivo_local(caminho: str) -> bytes:
    with open(caminho, "rb") as f:
        return f.read()


def listar_local(pasta: str, ignorar_pastas: Set[str]) -> Tuple[List[ArquivoOrigem], List[str]]:
    arquivos: List[ArquivoOrigem] = []
    ignorados: List[str] = []
    for raiz, subpastas, nomes in os.walk(pasta):
        subpastas[:] = sorted(d for d in subpastas if d.lower() not in ignorar_pastas)
        for nome in sorted(nomes):
            caminho = os.path.join(raiz, nome)
            relativo = os.path.relpath(caminho, pasta).replace(os.sep, "/")
            if _nome_ignoravel(nome) or os.path.splitext(nome)[1].lower() not in EXTENSOES_ORIGEM:
                ignorados.append(relativo)
                continue
            st = os.stat(caminho)
            arquivos.append(ArquivoOrigem(
                id=relativo,
                nome=relativo,
                md5=_md5_arquivo(caminho),
                tamanho=st.st_size,
                modificado_em=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
                ler_bytes=lambda c=caminho: _ler_arquivo_local(c),
            ))
    return arquivos, ignorados


# --- Google Drive ----------------------------------------------------
def criar_servico_drive():
    """Cria o cliente da API do Google Drive (somente leitura) usando as
    credenciais padrão do ambiente (Application Default Credentials).
    Na nuvem, são as da conta de serviço configurada no GitHub Actions."""
    import google.auth
    from googleapiclient.discovery import build

    credenciais, _ = google.auth.default(scopes=SCOPES_DRIVE)
    servico = build("drive", "v3", credentials=credenciais, cache_discovery=False)
    email = getattr(credenciais, "service_account_email", None)
    return servico, email


def verificar_pasta_drive(servico, pasta_id: str, email_conta: Optional[str]) -> str:
    """Confirma que a conta enxerga a pasta. Sem essa checagem, uma pasta
    não compartilhada com a conta de serviço apareceria apenas como
    'vazia'."""
    try:
        pasta = servico.files().get(
            fileId=pasta_id, fields="id, name, mimeType", supportsAllDrives=True
        ).execute(num_retries=3)
    except Exception as erro:  # HttpError 404/403
        conta = email_conta or "a conta usada pela rotina"
        raise RuntimeError(
            f"Não foi possível acessar a pasta do Drive (ID {pasta_id}). "
            f"Compartilhe a pasta com {conta} (permissão de leitor) e "
            f"confirme o ID da pasta. Detalhe: {erro}"
        ) from erro
    if pasta.get("mimeType") != MIME_PASTA_DRIVE:
        raise RuntimeError(f"O ID informado ({pasta_id}) não é de uma pasta do Drive.")
    return pasta.get("name", pasta_id)


def baixar_drive(servico, arquivo_id: str) -> bytes:
    return servico.files().get_media(
        fileId=arquivo_id, supportsAllDrives=True
    ).execute(num_retries=3)


def listar_drive(servico, pasta_raiz_id: str, ignorar_pastas: Set[str]) -> Tuple[List[ArquivoOrigem], List[str]]:
    """Lista, recursivamente, os arquivos .csv/.xlsx/.zip da pasta."""
    arquivos: List[ArquivoOrigem] = []
    ignorados: List[str] = []
    fila: List[Tuple[str, str]] = [(pasta_raiz_id, "")]
    while fila:
        pasta_id, prefixo = fila.pop(0)
        token = None
        while True:
            resposta = servico.files().list(
                q=f"'{pasta_id}' in parents and trashed = false",
                fields="nextPageToken, files(id, name, mimeType, md5Checksum, size, modifiedTime)",
                pageSize=1000,
                pageToken=token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            ).execute(num_retries=3)
            for item in resposta.get("files", []):
                nome = item["name"]
                if item["mimeType"] == MIME_PASTA_DRIVE:
                    if nome.lower() not in ignorar_pastas:
                        fila.append((item["id"], f"{prefixo}{nome}/"))
                    continue
                if _nome_ignoravel(nome) or os.path.splitext(nome)[1].lower() not in EXTENSOES_ORIGEM:
                    ignorados.append(f"{prefixo}{nome}")
                    continue
                md5 = item.get("md5Checksum") or f"{item.get('size', '0')}-{item['modifiedTime']}"
                arquivos.append(ArquivoOrigem(
                    id=item["id"],
                    nome=f"{prefixo}{nome}",
                    md5=md5,
                    tamanho=int(item.get("size") or 0),
                    modificado_em=pd.to_datetime(item["modifiedTime"], utc=True).to_pydatetime(),
                    ler_bytes=lambda i=item["id"]: baixar_drive(servico, i),
                ))
            token = resposta.get("nextPageToken")
            if not token:
                break
    return arquivos, ignorados


# ---------------------------------------------------------------------
# TABELA DE CONTROLE (arquivos já processados)
# ---------------------------------------------------------------------
def garantir_controle(client) -> None:
    ref = bigquery.DatasetReference(PROJECT_ID, DATASET_ID).table("CONTROLE_CARGA")
    try:
        client.get_table(ref)
    except NotFound:
        client.create_table(bigquery.Table(ref, schema=SCHEMA_CONTROLE))
        log("Tabela criada: CONTROLE_CARGA")


def ler_controle(client) -> Set[Tuple[str, str]]:
    """Retorna o conjunto (arquivo_id, md5) dos arquivos já processados."""
    try:
        df = client.query(
            f"SELECT arquivo_id, arquivo_md5 FROM `{TABELA_CONTROLE}` "
            f"WHERE status IN ('carregado', 'ignorado')"
        ).to_dataframe()
    except NotFound:
        return set()
    return set(zip(df["arquivo_id"].astype(str), df["arquivo_md5"].astype(str)))


def registrar_controle(client, arq: ArquivoOrigem, origem: str, resultado: Dict) -> None:
    registro = pd.DataFrame([{
        "arquivo_id": arq.id,
        "arquivo_nome": arq.nome,
        "arquivo_md5": arq.md5,
        "arquivo_tamanho": int(arq.tamanho),
        "arquivo_modificado_em": pd.Timestamp(arq.modificado_em),
        "origem": origem,
        "status": resultado["status"],
        "linhas_lidas": int(resultado["linhas_lidas"]),
        "ofertas_inseridas": int(resultado["ofertas_inseridas"]),
        "detalhe": resultado.get("detalhe", ""),
        "processado_em": pd.Timestamp.now(tz="UTC"),
    }])
    job = client.load_table_from_dataframe(
        registro, TABELA_CONTROLE,
        job_config=bigquery.LoadJobConfig(
            schema=SCHEMA_CONTROLE, write_disposition="WRITE_APPEND"
        ),
    )
    job.result()


# ---------------------------------------------------------------------
# LEITURA E CARGA
# ---------------------------------------------------------------------
def materializar(nome: str, conteudo: bytes, pasta_tmp: str) -> List[Tuple[str, str]]:
    """Grava o arquivo em disco (extraindo os .csv/.xlsx de um .zip) e
    retorna a lista (rótulo, caminho)."""
    if os.path.splitext(nome)[1].lower() != ".zip":
        destino = os.path.join(pasta_tmp, os.path.basename(nome))
        with open(destino, "wb") as f:
            f.write(conteudo)
        return [(nome, destino)]

    membros: List[Tuple[str, str]] = []
    with zipfile.ZipFile(io.BytesIO(conteudo)) as zf:
        for i, info in enumerate(sorted(zf.infolist(), key=lambda m: m.filename)):
            base = os.path.basename(info.filename)
            if (info.is_dir() or not base or _nome_ignoravel(base)
                    or "__MACOSX" in info.filename
                    or os.path.splitext(base)[1].lower() not in EXTENSOES_DADOS):
                continue
            # Apenas o nome-base é usado ao gravar, impedindo que um
            # caminho malicioso dentro do .zip escreva fora da pasta.
            destino = os.path.join(pasta_tmp, f"{i:03d}_{base}")
            with zf.open(info) as origem, open(destino, "wb") as saida:
                saida.write(origem.read())
            membros.append((f"{nome}::{info.filename}", destino))
    return membros


def _ler_bruto(caminho: str) -> pd.DataFrame:
    if caminho.lower().endswith(".xlsx"):
        return pd.read_excel(caminho, dtype=str)
    return pd.read_csv(caminho, sep=cd._detectar_separador_csv(caminho), dtype=str)


def preparar_fatias(caminho: str, pasta_tmp: str) -> Tuple[List[str], Set[str]]:
    """Valida as colunas e, se o arquivo abranger mais de um dia de
    coleta, divide-o em um arquivo por dia. Retorna (caminhos, colunas
    ausentes)."""
    bruto = _ler_bruto(caminho)
    faltando = COLUNAS_ESPERADAS - set(bruto.columns)
    if faltando:
        return [], faltando
    if bruto.empty:
        return [], set()

    dias = pd.to_datetime(bruto["data_coleta"], errors="coerce").dt.date
    dias_unicos = sorted(d for d in dias.dropna().unique())
    if len(dias_unicos) <= 1:
        return [caminho], set()

    invalidas = int(dias.isna().sum())
    log(f"  O arquivo abrange {len(dias_unicos)} dias de coleta; carregando um dia por vez."
        + (f" ({invalidas} linha(s) com data inválida descartada(s).)" if invalidas else ""))
    base = os.path.splitext(os.path.basename(caminho))[0]
    fatias: List[str] = []
    for dia in dias_unicos:
        destino = os.path.join(pasta_tmp, f"{base}__{dia}.csv")
        bruto[dias == dia].to_csv(destino, sep=";", index=False, encoding="utf-8")
        fatias.append(destino)
    return fatias, set()


def _normalizar_datas(serie: pd.Series) -> pd.Series:
    """Datas sem fuso, em UTC (o BigQuery interpreta datas sem fuso como
    UTC ao carregar a coluna TIMESTAMP)."""
    return pd.to_datetime(serie, utc=True).dt.tz_localize(None)


def _chaves_oferta(ids: pd.Series, lojas: pd.Series, datas: pd.Series) -> pd.Series:
    return (
        ids.astype(str).str.strip() + "|"
        + lojas.astype("Int64").astype(str) + "|"
        + _normalizar_datas(datas).dt.strftime("%Y-%m-%d %H:%M:%S.%f")
    )


def filtrar_ofertas_novas(client, df: pd.DataFrame, mapa_lojas: Dict) -> pd.DataFrame:
    """Remove do DataFrame as ofertas cuja chave natural (produto, loja,
    data_coleta) já existe na tabela OFERTA."""
    id_loja = df["nome_loja"].apply(cd.padronizar_loja).map(mapa_lojas)
    chaves = _chaves_oferta(df["id_produto_externo"], id_loja, df["data_coleta"])

    datas = _normalizar_datas(df["data_coleta"])
    inicio = datas.min().to_pydatetime().replace(tzinfo=timezone.utc)
    fim = datas.max().to_pydatetime().replace(tzinfo=timezone.utc)
    config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("inicio", "TIMESTAMP", inicio),
        bigquery.ScalarQueryParameter("fim", "TIMESTAMP", fim),
    ])
    existentes = client.query(
        f"SELECT id_produto, id_loja, data_coleta FROM `{TABELA_OFERTA}` "
        f"WHERE data_coleta BETWEEN @inicio AND @fim",
        job_config=config,
    ).to_dataframe()
    if existentes.empty:
        return df

    chaves_existentes = set(
        _chaves_oferta(existentes["id_produto"], existentes["id_loja"], existentes["data_coleta"])
    )
    return df[~chaves.isin(chaves_existentes)].reset_index(drop=True)


def carregar_dataframe(client, df: pd.DataFrame) -> int:
    """Carrega PRODUTO, LOJA e OFERTA a partir de um DataFrame já limpo.
    Retorna a quantidade de ofertas inseridas."""
    cd.carregar_produto(client, df)
    mapa_lojas = cd.carregar_loja(client, df)
    novas = filtrar_ofertas_novas(client, df, mapa_lojas)
    descartadas = len(df) - len(novas)
    if descartadas:
        print(f"OFERTA: {descartadas} oferta(s) já existiam na tabela e foram desconsideradas.")
    if novas.empty:
        print("OFERTA: nenhuma oferta nova a inserir.")
        return 0
    cd.carregar_oferta(client, novas, mapa_lojas)
    return len(novas)


def processar_arquivo(client, arq: ArquivoOrigem) -> Dict:
    conteudo = arq.ler_bytes()
    lidas = inseridas = usados = 0
    notas: List[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        membros = materializar(arq.nome, conteudo, tmp)
        if not membros:
            return {"status": "ignorado", "linhas_lidas": 0, "ofertas_inseridas": 0,
                    "detalhe": "nenhum arquivo .csv/.xlsx encontrado"}

        for rotulo, caminho in membros:
            log(f"  Lendo: {rotulo}")
            fatias, faltando = preparar_fatias(caminho, tmp)
            if faltando:
                nota = f"{rotulo}: colunas ausentes {sorted(faltando)}"
                log(f"  Ignorado — {nota}")
                notas.append(nota)
                continue
            if not fatias:
                notas.append(f"{rotulo}: sem linhas")
                continue
            for fatia in fatias:
                df = cd.ler_arquivo_entrada(fatia)
                if df.empty:
                    continue
                usados += 1
                lidas += len(df)
                inseridas += carregar_dataframe(client, df)

    detalhe = "; ".join(notas)[:500]
    if usados == 0:
        return {"status": "ignorado", "linhas_lidas": 0, "ofertas_inseridas": 0, "detalhe": detalhe}
    return {"status": "carregado", "linhas_lidas": lidas,
            "ofertas_inseridas": inseridas, "detalhe": detalhe}


def avisar_expiracao(client) -> None:
    """Informa, no log, se o dataset ou as tabelas possuem expiração
    automática. O BigQuery Sandbox (projeto sem faturamento) aplica, por
    padrão, expiração de 60 dias a tabelas e partições; sem ajuste, o
    histórico de preços seria apagado automaticamente. A checagem é
    apenas informativa e nunca interrompe a execução."""
    avisos: List[str] = []
    try:
        dataset = client.get_dataset(bigquery.DatasetReference(PROJECT_ID, DATASET_ID))
        if getattr(dataset, "default_table_expiration_ms", None):
            avisos.append("expiração padrão das tabelas do dataset: "
                          f"{dataset.default_table_expiration_ms / 86_400_000:.0f} dias")
        if getattr(dataset, "default_partition_expiration_ms", None):
            avisos.append("expiração padrão das partições do dataset: "
                          f"{dataset.default_partition_expiration_ms / 86_400_000:.0f} dias")
        for nome in ("PRODUTO", "LOJA", "OFERTA", "CONTROLE_CARGA"):
            try:
                tabela = client.get_table(
                    bigquery.DatasetReference(PROJECT_ID, DATASET_ID).table(nome))
            except NotFound:
                continue
            if getattr(tabela, "expires", None):
                avisos.append(f"a tabela {nome} expira em {tabela.expires:%d/%m/%Y}")
            particao = getattr(tabela, "time_partitioning", None)
            if particao is not None and getattr(particao, "expiration_ms", None):
                avisos.append(f"as partições de {nome} expiram após "
                              f"{particao.expiration_ms / 86_400_000:.0f} dias")
    except Exception as erro:  # informativo: nunca bloqueia a rotina
        log(f"(Não foi possível verificar a expiração das tabelas: {erro})")
        return
    if avisos:
        log("ATENÇÃO — expiração automática de dados detectada no BigQuery:")
        for aviso in avisos:
            log(f"  - {aviso}")
        log("  Dados expirados são apagados definitivamente. Veja como remover a "
            "expiração antes que o histórico seja perdido.")


def ha_pendencias_de_tratamento(client) -> bool:
    try:
        df = client.query(
            f"SELECT COUNT(*) AS n FROM `{TABELA_OFERTA}` WHERE situacao_compra IS NULL"
        ).to_dataframe()
    except NotFound:
        return False
    return int(df["n"].iloc[0]) > 0


# ---------------------------------------------------------------------
# EXECUÇÃO
# ---------------------------------------------------------------------
def _parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Rotina diária: lê os arquivos novos da pasta (Drive ou local), "
                    "carrega no BigQuery e executa o tratamento."
    )
    p.add_argument("--origem", choices=["drive", "local"],
                   help="Origem dos arquivos. Padrão: 'drive' se houver ID de pasta "
                        "(--pasta-id ou variável DRIVE_FOLDER_ID); caso contrário, 'local'.")
    p.add_argument("--pasta-id", help="ID da pasta do Google Drive (ou variável DRIVE_FOLDER_ID).")
    p.add_argument("--pasta", default=".", help="Pasta local (origem 'local'). Padrão: pasta atual.")
    p.add_argument("--ignorar-pastas", default="",
                   help="Nomes de subpastas a ignorar, separados por vírgula (ex.: backup,teste).")
    p.add_argument("--simular", action="store_true",
                   help="Apenas lista o que seria carregado; não baixa nem grava nada.")
    p.add_argument("--limite", type=int, default=0,
                   help="Processa no máximo N arquivos novos nesta execução (0 = todos).")
    p.add_argument("--sem-tratamento", action="store_true",
                   help="Carrega os arquivos, mas não executa tratar_dados.")
    return p.parse_args(argv)


def main(argv=None) -> int:
    # Evita que um caractere fora da codificação do console/arquivo de log
    # (comum no Windows) interrompa a execução.
    for fluxo in (sys.stdout, sys.stderr):
        try:
            fluxo.reconfigure(errors="replace")
        except Exception:
            pass

    args = _parse_args(argv)
    ignorar = {n.strip().lower() for n in args.ignorar_pastas.split(",") if n.strip()}

    pasta_id = args.pasta_id or os.environ.get("DRIVE_FOLDER_ID")
    origem = args.origem or ("drive" if pasta_id else "local")

    log(f"Rotina diária iniciada (origem: {origem}{', SIMULAÇÃO' if args.simular else ''}).")

    if origem == "drive":
        if not pasta_id:
            log("ERRO: informe o ID da pasta (--pasta-id ou variável DRIVE_FOLDER_ID).")
            return 1
        try:
            servico, email = criar_servico_drive()
        except ImportError:
            log("ERRO: bibliotecas do Google Drive ausentes. Instale com: "
                "pip install google-api-python-client google-auth")
            return 1
        nome_pasta = verificar_pasta_drive(servico, pasta_id, email)
        log(f"Pasta do Drive: {nome_pasta}")
        arquivos, ignorados = listar_drive(servico, pasta_id, ignorar)
    else:
        log(f"Pasta local: {os.path.abspath(args.pasta)}")
        arquivos, ignorados = listar_local(args.pasta, ignorar)

    log(f"{len(arquivos)} arquivo(s) .csv/.xlsx/.zip encontrado(s)"
        + (f"; {len(ignorados)} item(ns) de outro tipo ignorado(s)." if ignorados else "."))

    client = cd.get_client()
    avisar_expiracao(client)
    if not args.simular:
        cd.garantir_tabelas(client)
        garantir_controle(client)

    processados = ler_controle(client)
    novos = sorted((a for a in arquivos if a.chave not in processados),
                   key=lambda a: (a.modificado_em, a.nome))
    log(f"{len(arquivos) - len(novos)} já processado(s); {len(novos)} novo(s).")

    if args.limite and len(novos) > args.limite:
        log(f"Limite de {args.limite} arquivo(s) por execução aplicado.")
        novos = novos[:args.limite]

    if args.simular:
        for arq in novos:
            log(f"  NOVO  {arq.modificado_em.strftime('%Y-%m-%d %H:%M')}  {arq.nome}  "
                f"({formatar_tamanho(arq.tamanho)})")
        log("Simulação concluída: nada foi baixado nem gravado.")
        return 0

    total_inseridas = 0
    erros: List[str] = []
    for n, arq in enumerate(novos, start=1):
        log(f"[{n}/{len(novos)}] {arq.nome} ({formatar_tamanho(arq.tamanho)})")
        try:
            resultado = processar_arquivo(client, arq)
            registrar_controle(client, arq, origem, resultado)
            total_inseridas += resultado["ofertas_inseridas"]
            log(f"  Concluído: {resultado['status']} — {resultado['linhas_lidas']} linha(s) lida(s), "
                f"{resultado['ofertas_inseridas']} oferta(s) inserida(s).")
        except Exception as erro:  # um arquivo com problema não impede os demais
            erros.append(f"{arq.nome}: {erro}")
            log(f"  FALHA em {arq.nome}: {erro}")
            traceback.print_exc()

    if not args.sem_tratamento and (total_inseridas > 0 or ha_pendencias_de_tratamento(client)):
        log("Executando o tratamento (tratar_dados)...")
        try:
            td.main()
        except Exception as erro:
            erros.append(f"tratamento: {erro}")
            log(f"  FALHA no tratamento: {erro}")
            traceback.print_exc()
    elif not args.sem_tratamento:
        log("Nenhuma oferta nova nem pendente: tratamento não é necessário.")

    log("")
    log("RESUMO DA EXECUÇÃO")
    log(f"  Arquivos novos processados: {len(novos) - len([e for e in erros if not e.startswith('tratamento')])}")
    log(f"  Ofertas inseridas em OFERTA: {total_inseridas}")
    if erros:
        log(f"  Falhas: {len(erros)}")
        for e in erros:
            log(f"    - {e}")
        return 1
    log("  Sem falhas.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
