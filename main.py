"""
╔══════════════════════════════════════════════════════════════════════════════╗
║          GERADOR DE CARD DE PROMOÇÃO — QUINELATO FREIOS                     ║
║                                                                              ║
║  Como usar:                                                                  ║
║    1. Execute: python -m streamlit run main.py                               ║
║    2. Edite os campos no painel lateral                                      ║
║    3. (Opcional) Envie a foto do produto                                     ║
║    4. Baixe o card em PNG ou JPG (300 dpi)                                   ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import streamlit as st
from PIL import Image, ImageDraw, ImageFont
import io
import os
import re
import zipfile
import mysql.connector
import requests
import base64
from google import genai as google_genai
from google.genai import types as genai_types
from dotenv import load_dotenv

load_dotenv()


def _converter_svg_para_png(svg_bytes: bytes, largura: int = 1200) -> bytes:
    """
    Converte bytes SVG para bytes PNG de alta resolução.
    Usa Playwright (Chromium headless) para renderizar o SVG —
    funciona no Windows sem dependências nativas.
    """
    import tempfile
    from playwright.sync_api import sync_playwright

    # Gravar SVG num arquivo temporário
    with tempfile.NamedTemporaryFile(suffix=".svg", delete=False, dir=".") as tmp:
        tmp.write(svg_bytes)
        tmp_path = tmp.name

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": largura, "height": largura})
            page.goto(f"file:///{os.path.abspath(tmp_path).replace(os.sep, '/')}")

            # Esperar o SVG carregar e pegar suas dimensões reais
            svg_el = page.query_selector("svg")
            if svg_el is None:
                raise ValueError("Não foi possível encontrar elemento SVG.")

            bbox = svg_el.bounding_box()
            if bbox:
                # Redimensionar viewport para o tamanho real do SVG
                page.set_viewport_size({
                    "width": max(1, int(bbox["width"])),
                    "height": max(1, int(bbox["height"])),
                })

            png_bytes = page.screenshot(type="png", omit_background=True)
            browser.close()

        return png_bytes
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _cfg(chave: str) -> str:
    """Lê configuração: tenta st.secrets primeiro, depois variável de ambiente."""
    try:
        return st.secrets[chave]
    except Exception:
        return os.getenv(chave, "")


# ──────────────────────────────────────────────────────────────────────────────
# BANCO DE DADOS — MySQL
# ──────────────────────────────────────────────────────────────────────────────

def buscar_produto(codigo_interno: str):
    """
    Busca o produto pelo código interno no MySQL.
    Retorna dict com os dados, {} se não encontrado, ou None em erro de conexão.
    """
    sql = """
        SELECT
            CodExibirGrid,
            DescricaoProdutoPT,
            Marca
        FROM ProdutosTraducao
        WHERE Codigo = %s
    """
    conn   = None
    cursor = None
    try:
        conn = mysql.connector.connect(
            host=_cfg("DB_SERVER"),
            port=int(_cfg("DB_PORT") or 3306),
            user=_cfg("DB_USER"),
            password=_cfg("DB_PASSWORD"),
            database=_cfg("DB_DATABASE"),
            connect_timeout=10,
            use_pure=True,
            auth_plugin="mysql_native_password",
        )
        cursor = conn.cursor(dictionary=True)
        cursor.execute(sql, (int(codigo_interno),))
        row = cursor.fetchone()
        if row:
            return {
                "num_fabricante": row.get("CodExibirGrid")      or "",
                "descricao":      row.get("DescricaoProdutoPT") or "",
                "imagem_marca":   row.get("Marca")              or "",
            }
        return {}
    except Exception as e:
        st.sidebar.error(f"Erro BD: {e}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn and conn.is_connected():
            conn.close()


# ──────────────────────────────────────────────────────────────────────────────
# BANCO DE DADOS — MARCAS
# ──────────────────────────────────────────────────────────────────────────────

def _conn_bd():
    """Abre e retorna uma conexão MySQL."""
    return mysql.connector.connect(
        host=_cfg("DB_SERVER"),
        port=int(_cfg("DB_PORT") or 3306),
        user=_cfg("DB_USER"),
        password=_cfg("DB_PASSWORD"),
        database=_cfg("DB_DATABASE"),
        connect_timeout=10,
        use_pure=True,
        auth_plugin="mysql_native_password",
    )



_SQL_CRIAR_MARCAS = """
    CREATE TABLE IF NOT EXISTS Marcas (
        id            INT AUTO_INCREMENT PRIMARY KEY,
        nome          VARCHAR(191) NOT NULL UNIQUE,
        logo_dados    LONGBLOB,
        logo_mime     VARCHAR(100) DEFAULT 'image/png',
        criado_em     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        atualizado_em TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                      ON UPDATE CURRENT_TIMESTAMP
    ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
"""


def buscar_marca(nome_marca: str):
    """
    Busca uma marca pelo nome exato na tabela Marcas.
    Cria a tabela automaticamente se ainda não existir.
    Retorna dict com id/nome/logo_dados/logo_mime, {} se não encontrada,
    ou None em caso de erro de conexão.
    """
    conn = cursor = None
    try:
        conn = _conn_bd()
        cursor = conn.cursor(dictionary=True)
        # Garante que a tabela existe antes de consultar
        cursor.execute(_SQL_CRIAR_MARCAS)
        conn.commit()
        cursor.execute(
            "SELECT id, nome, logo_dados, logo_mime FROM Marcas WHERE nome = %s",
            (nome_marca,),
        )
        row = cursor.fetchone()
        if row:
            return {
                "id":         row["id"],
                "nome":       row["nome"],
                "logo_dados": row.get("logo_dados"),
                "logo_mime":  row.get("logo_mime") or "image/png",
            }
        return {}
    except Exception as e:
        st.sidebar.error(f"Erro BD (marcas): {e}")
        return None
    finally:
        if cursor: cursor.close()
        if conn and conn.is_connected(): conn.close()


def cadastrar_marca(nome: str, logo_dados: bytes, logo_mime: str = "image/png") -> bool:
    """
    Cadastra ou atualiza uma marca na tabela Marcas.
    Cria a tabela automaticamente se ainda não existir.
    Usa INSERT … ON DUPLICATE KEY UPDATE para não gerar duplicatas.
    Retorna True se bem-sucedido.
    """
    conn = cursor = None
    try:
        conn = _conn_bd()
        cursor = conn.cursor()
        # Garante que a tabela existe antes de inserir
        cursor.execute(_SQL_CRIAR_MARCAS)
        conn.commit()
        cursor.execute(
            """
            INSERT INTO Marcas (nome, logo_dados, logo_mime)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE
                logo_dados    = VALUES(logo_dados),
                logo_mime     = VALUES(logo_mime),
                atualizado_em = CURRENT_TIMESTAMP
            """,
            (nome, logo_dados, logo_mime),
        )
        conn.commit()
        return True
    except Exception as e:
        st.error(f"Erro ao salvar marca: {e}")
        return False
    finally:
        if cursor: cursor.close()
        if conn and conn.is_connected(): conn.close()


def listar_marcas() -> list[str]:
    """
    Retorna lista com os nomes de todas as marcas cadastradas na tabela Marcas,
    ordenadas alfabeticamente. Retorna lista vazia em caso de erro.
    """
    conn = cursor = None
    try:
        conn = _conn_bd()
        cursor = conn.cursor()
        cursor.execute(_SQL_CRIAR_MARCAS)
        conn.commit()
        cursor.execute("SELECT nome FROM Marcas ORDER BY nome ASC")
        return [row[0] for row in cursor.fetchall()]
    except Exception:
        return []
    finally:
        if cursor: cursor.close()
        if conn and conn.is_connected(): conn.close()


_BASE_URL_IMAGENS = "https://www.grupodinatec.com.br/imagens"


_HEADERS_NAVEGADOR = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Connection": "keep-alive",
}


def buscar_imagens_produto(codigo: str) -> list[dict]:
    """
    Tenta baixar as variantes _1 a _5 da imagem do produto.
    Estratégia 1: FTP (credenciais configuradas) — evita bot-protection HTTP.
    Estratégia 2: HTTP com headers de navegador (fallback).
    Valida com PIL antes de aceitar — garante que são bytes de imagem real.
    Retorna lista de dicts com 'label', 'url' e 'bytes'.
    """
    import ftplib as _ftplib

    encontradas = []

    # ── Estratégia 1: FTP ────────────────────────────────────────────────────
    ftp_host     = _cfg("FTP_HOST")
    ftp_port     = int(_cfg("FTP_PORT") or 21)
    ftp_user     = _cfg("FTP_USER")
    ftp_password = _cfg("FTP_PASSWORD")
    ftp_pasta    = _cfg("FTP_PASTA") or "/imagens"

    if ftp_host and ftp_user:
        try:
            _ftp = _ftplib.FTP()
            _ftp.connect(ftp_host, ftp_port, timeout=20)
            _ftp.login(ftp_user, ftp_password)
            try:
                _ftp.cwd(ftp_pasta)
            except _ftplib.error_perm:
                pass  # pasta não existe — nenhuma imagem no FTP

            for i in range(1, 6):
                nome_arq = f"{codigo}_{i}.jpg"
                buf = io.BytesIO()
                try:
                    _ftp.retrbinary(f"RETR {nome_arq}", buf.write)
                    raw = buf.getvalue()
                    if len(raw) < 1024:
                        continue
                    _img_test = Image.open(io.BytesIO(raw))
                    _img_test.load()
                    url = f"{_BASE_URL_IMAGENS}/{nome_arq}"
                    encontradas.append({"label": f"Imagem {i}", "url": url, "bytes": raw})
                except Exception:
                    continue

            _ftp.quit()
        except Exception:
            pass  # FTP indisponível — tenta HTTP abaixo

    if encontradas:
        return encontradas

    # ── Estratégia 2: HTTP ───────────────────────────────────────────────────
    for i in range(1, 6):
        url = f"{_BASE_URL_IMAGENS}/{codigo}_{i}.jpg"
        try:
            resp = requests.get(url, headers=_HEADERS_NAVEGADOR,
                                timeout=10, allow_redirects=True)
            if resp.status_code != 200 or len(resp.content) < 1024:
                continue
            ct = resp.headers.get("Content-Type", "")
            if "text" in ct or "html" in ct:
                continue  # bot-protection page, não é imagem
            _img_test = Image.open(io.BytesIO(resp.content))
            _img_test.load()
            encontradas.append({"label": f"Imagem {i}", "url": url, "bytes": resp.content})
        except Exception:
            continue

    return encontradas


def _nome_arquivo(codigo: str) -> str:
    """Remove caracteres inválidos e retorna nome seguro para download."""
    nome = re.sub(r"[^\w]", "_", codigo)
    nome = re.sub(r"_+", "_", nome).strip("_")
    return nome or "promo"

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURAÇÃO DE FONTES
# O programa testa cada caminho em ordem e usa o primeiro que existir.
# Prioridade: Windows → Linux. Fallback: fonte embutida do Pillow.
# ──────────────────────────────────────────────────────────────────────────────
_FONTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

FONTS_BOLD_ITALIC = [
    os.path.join(_FONTS_DIR, "arialbi.ttf"),                               # Projeto
    "C:/Windows/Fonts/arialbi.ttf",                                        # Windows
    "C:/Windows/Fonts/calibriz.ttf",                                       # Windows
    "/usr/share/fonts/truetype/liberation/LiberationSans-BoldItalic.ttf",  # Linux
]
FONTS_BOLD = [
    os.path.join(_FONTS_DIR, "arialbd.ttf"),                               # Projeto
    "C:/Windows/Fonts/arialbd.ttf",                                        # Windows
    "C:/Windows/Fonts/calibrib.ttf",                                       # Windows
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",        # Linux
]
FONTS_REGULAR = [
    os.path.join(_FONTS_DIR, "arial.ttf"),                                 # Projeto
    "C:/Windows/Fonts/arial.ttf",                                          # Windows
    "C:/Windows/Fonts/calibri.ttf",                                        # Windows
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",     # Linux
]

_DIR = os.path.dirname(os.path.abspath(__file__))


# ══════════════════════════════════════════════════════════════════════════════
# FUNÇÕES AUXILIARES
# ══════════════════════════════════════════════════════════════════════════════

def carregar_fonte(candidatos, tamanho):
    """
    Tenta carregar uma fonte TrueType a partir de uma lista de caminhos.
    Retorna a primeira fonte encontrada, ou a fonte padrão (bitmap) do Pillow
    caso nenhum arquivo seja encontrado.

    Parâmetros:
        candidatos : list[str] — lista de caminhos possíveis para o .ttf
        tamanho    : int       — tamanho da fonte em pontos
    """
    for caminho in candidatos:
        if os.path.exists(caminho):
            try:
                return ImageFont.truetype(caminho, tamanho)
            except Exception:
                continue
    return ImageFont.load_default()


def retangulo_arredondado(draw, xy, raio, cor):
    """
    Desenha um retângulo com bordas arredondadas.
    Usa a API nativa do Pillow (>=8.2); faz fallback para retângulo simples
    em versões antigas.

    Parâmetros:
        draw : ImageDraw — contexto de desenho ativo
        xy   : list/tuple [x0, y0, x1, y1] — coordenadas do retângulo
        raio : int  — raio dos cantos em pixels
        cor  : tuple — cor de preenchimento (R, G, B)
    """
    x0, y0, x1, y1 = [int(v) for v in xy]
    try:
        draw.rounded_rectangle([x0, y0, x1, y1], radius=int(raio), fill=cor)
    except AttributeError:
        # Fallback para Pillow < 8.2
        draw.rectangle([x0 + raio, y0, x1 - raio, y1], fill=cor)
        draw.rectangle([x0, y0 + raio, x1, y1 - raio], fill=cor)
        for cx, cy in [(x0, y0), (x1 - 2 * raio, y0),
                       (x0, y1 - 2 * raio), (x1 - 2 * raio, y1 - 2 * raio)]:
            draw.ellipse([cx, cy, cx + 2 * raio, cy + 2 * raio], fill=cor)


def remover_fundo_branco(img: Image.Image, tolerancia: int = 25) -> Image.Image:
    """
    Torna transparentes os pixels brancos/quase-brancos de uma imagem RGBA.

    Parâmetros:
        img        : PIL.Image — imagem de entrada (qualquer modo)
        tolerancia : int — quanto cada canal R/G/B pode se afastar de 255
                     e ainda ser considerado "branco" (padrão 25)

    Retorna:
        PIL.Image em modo RGBA com o fundo removido.
    """
    img  = img.convert("RGBA")
    px   = img.load()
    w, h = img.size
    lim  = 255 - tolerancia
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if r >= lim and g >= lim and b >= lim:
                px[x, y] = (r, g, b, 0)
    return img


def centralizar_texto(draw, texto, fonte, caixa, cor):
    """
    Desenha um texto centralizado (horizontal e verticalmente) dentro de
    uma caixa retangular.

    Parâmetros:
        draw  : ImageDraw
        texto : str   — texto a desenhar
        fonte : ImageFont
        caixa : list [x0, y0, x1, y1] — área de destino
        cor   : tuple — cor do texto (R, G, B)
    """
    x0, y0, x1, y1 = [int(v) for v in caixa]
    bb  = draw.textbbox((0, 0), texto, font=fonte)
    tw  = bb[2] - bb[0]
    th  = bb[3] - bb[1]
    draw.text(
        (x0 + (x1 - x0 - tw) // 2 - bb[0],
         y0 + (y1 - y0 - th) // 2 - bb[1]),
        texto, font=fonte, fill=cor
    )


def quebrar_linhas(draw, texto, fonte, largura_max):
    """
    Quebra o texto em linhas que cabem dentro de largura_max pixels.
    Respeita espaços como pontos de quebra.

    Retorna lista de strings (uma por linha).
    """
    palavras    = texto.split()
    linhas      = []
    linha_atual = ""
    for palavra in palavras:
        teste = (linha_atual + " " + palavra).strip()
        bb    = draw.textbbox((0, 0), teste, font=fonte)
        if bb[2] - bb[0] <= largura_max:
            linha_atual = teste
        else:
            if linha_atual:
                linhas.append(linha_atual)
            linha_atual = palavra
    if linha_atual:
        linhas.append(linha_atual)
    return linhas or [texto]


def fonte_auto_tamanho(draw, texto, candidatos, largura_max, tam_inicial, tam_min=10):
    """
    Retorna a maior fonte (da lista 'candidatos') cujo texto caiba dentro de
    'largura_max' pixels. Começa em 'tam_inicial' e vai diminuindo de 2 em 2.

    Parâmetros:
        draw        : ImageDraw
        texto       : str
        candidatos  : list[str] — caminhos de fontes
        largura_max : int — largura máxima em pixels
        tam_inicial : int — tamanho inicial tentado
        tam_min     : int — tamanho mínimo garantido
    """
    for tamanho in range(int(tam_inicial), tam_min - 1, -2):
        fonte = carregar_fonte(candidatos, tamanho)
        bb    = draw.textbbox((0, 0), texto, font=fonte)
        if (bb[2] - bb[0]) <= largura_max:
            return fonte
    return carregar_fonte(candidatos, tam_min)


# ══════════════════════════════════════════════════════════════════════════════
# FUNÇÃO PRINCIPAL DE GERAÇÃO DO CARD
# ══════════════════════════════════════════════════════════════════════════════

def gerar_rodape(W, site, telefone, whatsapp, fator_s, endereco=""):
    """
    Gera a faixa de rodapé personalizada com a logo da empresa e contatos.

    Layout:
        ┌─────────────────────────────────────────────────────────┐
        │  [linha separadora cinza]                               │
        │                                                         │
        │    [LOGO da empresa]    │  www.site.com.br              │
        │                        │  Tel: (xx) xxxx-xxxx           │
        │                        │  WhatsApp: (xx) xxxxx-xxxx     │
        │                                                         │
        └─────────────────────────────────────────────────────────┘

    Parâmetros:
        W        : int  — largura do card em pixels
        site     : str  — endereço do site
        telefone : str  — número de telefone
        whatsapp : str  — número do WhatsApp
        fator_s  : float — fator de escala uniforme do card

    Retorna:
        PIL.Image.Image — faixa do rodapé em modo RGB
    """
    def cs(v): return int(v * fator_s)

    LOGO_PATH  = os.path.join(_DIR, "logo.png")
    LOGO_MAX_W = int(W * 0.38)   # logo ocupa no máximo 38% da largura
    LOGO_MAX_H = cs(90)          # altura máxima da logo
    GAP        = cs(32)          # espaço entre logo e bloco de texto
    LINHA_SP   = cs(8)           # espaço entre linhas de texto
    PAD_V      = cs(22)          # espaço vertical acima e abaixo do conteúdo

    fonte_label = carregar_fonte(FONTS_BOLD,    cs(17))
    fonte_valor = carregar_fonte(FONTS_REGULAR, cs(17))
    fonte_end   = carregar_fonte(FONTS_BOLD,    cs(14))

    # Endereço vai abaixo da logo — 1ª linha apenas para não estourar
    _end_linha = (endereco.split("\n")[0].strip()) if endereco else ""

    # Contatos (sem endereço — ele fica abaixo da logo)
    contatos = [(l, v.strip()) for l, v in
                [("Site", site), ("Tel", telefone), ("WhatsApp", whatsapp)]
                if v and v.strip()]

    # ── Carregar e escalar a logo ─────────────────────────────────────────────
    logo_img = None
    logo_w = logo_h = 0
    if os.path.exists(LOGO_PATH):
        logo_img = Image.open(LOGO_PATH).convert("RGBA")
        escala   = min(LOGO_MAX_W / logo_img.width, LOGO_MAX_H / logo_img.height)
        logo_w   = max(1, int(logo_img.width  * escala))
        logo_h   = max(1, int(logo_img.height * escala))
        logo_img = logo_img.resize((logo_w, logo_h), Image.LANCZOS)

    # ── Medir endereço e bloco de texto ──────────────────────────────────────
    draw_tmp = ImageDraw.Draw(Image.new("RGB", (10, 10)))

    GAP_END = cs(5)
    if _end_linha:
        bb_end  = draw_tmp.textbbox((0, 0), _end_linha, font=fonte_end)
        end_h   = bb_end[3] - bb_end[1]
        end_w   = bb_end[2] - bb_end[0]
    else:
        end_h = end_w = 0
        GAP_END = 0

    # Altura da coluna esquerda: logo + gap + endereço
    logo_col_h = logo_h + GAP_END + end_h

    linha_h = draw_tmp.textbbox((0, 0), "Ag", font=fonte_valor)[3]
    texto_w = 0
    if contatos:
        for label, valor in contatos:
            bb = draw_tmp.textbbox((0, 0), f"{label}: {valor}", font=fonte_label)
            texto_w = max(texto_w, bb[2] - bb[0])
    texto_h = len(contatos) * (linha_h + LINHA_SP) - LINHA_SP if contatos else 0

    # ── Calcular dimensões totais do rodapé ───────────────────────────────────
    bloco_w = logo_w + (GAP + texto_w if contatos else 0)
    bloco_h = max(logo_col_h, texto_h)
    TOTAL_H = bloco_h + 2 * PAD_V + cs(4)   # +4 = linha separadora

    # ── Criar canvas do rodapé ───────────────────────────────────────────────
    rodape = Image.new("RGB", (W, TOTAL_H), (41, 45, 150))
    draw   = ImageDraw.Draw(rodape)

    # Linha separadora no topo (cinza claro)
    draw.rectangle([0, 0, W, cs(3)], fill=(184, 184, 184))

    # Origem do bloco (centralizado horizontalmente e verticalmente)
    origem_x = (W - bloco_w) // 2
    origem_y = cs(4) + (TOTAL_H - cs(4) - bloco_h) // 2

    # ── Logo + endereço (coluna esquerda) ─────────────────────────────────────
    if logo_img:
        # Alinhar bloco logo+endereço verticalmente ao centro da coluna
        bloco_esq_y = origem_y + (bloco_h - logo_col_h) // 2

        # Logo centralizada horizontalmente dentro da largura logo_w
        _lx = origem_x + (logo_w - logo_img.width) // 2
        rodape.paste(logo_img, (_lx, bloco_esq_y), logo_img)

        # Endereço abaixo da logo, centralizado na largura total do rodapé e negrito
        if _end_linha:
            _ey = bloco_esq_y + logo_h + GAP_END + cs(14)
            _ex = (W - end_w) // 2
            draw.text((_ex, _ey - bb_end[1]), _end_linha,
                      font=fonte_end, fill=(255, 255, 255))

    # ── Texto de contatos centralizado verticalmente no bloco ─────────────────
    if contatos:
        txt_x       = origem_x + logo_w + GAP
        bloco_txt_h = len(contatos) * (linha_h + LINHA_SP) - LINHA_SP
        y_txt       = origem_y + (bloco_h - bloco_txt_h) // 2

        for label, valor in contatos:
            bb_label = draw.textbbox((0, 0), f"{label}: ", font=fonte_label)
            lw       = bb_label[2] - bb_label[0]
            draw.text((txt_x,      y_txt - bb_label[1]),
                      f"{label}: ", font=fonte_label, fill=(255, 255, 255))
            draw.text((txt_x + lw, y_txt - bb_label[1]),
                      valor,        font=fonte_valor,  fill=(255, 255, 255))
            y_txt += linha_h + LINHA_SP

    return rodape


def gerar_card(badge, codigo, nome, veiculos,
               foto_produto=None,
               site="", telefone="", whatsapp="",
               imagem_marca="",
               marca_logo_bytes: bytes | None = None,
               endereco="",
               codigo_interno=""):
    """
    Gera o card de promoção a partir de um canvas branco.

    Fluxo:
        1. Cria canvas branco (913×915 px)
        2. Desenha o badge (selo vermelho) com o texto informado
        3. Redesenha a barra de código + nome do produto
        4. (Se fornecida) Cola a foto do produto centralizada na área do meio
        5. Desenha "Veículos" e o valor logo abaixo da foto
        6. Gera o rodapé personalizado (logo + contatos)
        7. Monta o canvas final: cabeçalho + foto + veículos + rodapé

    Parâmetros:
        badge        : str — texto do selo vermelho (ex: "LANÇAMENTO")
        codigo       : str — código do produto (ex: "QA-1077")
        nome         : str — nome completo do produto
        veiculos     : str — veículos compatíveis (ex: "DAF / Volvo")
        foto_produto : file-like | None — foto enviada pelo usuário
        site         : str — endereço do site da empresa
        telefone     : str — telefone de contato
        whatsapp     : str — número do WhatsApp

    Retorna:
        PIL.Image.Image — card finalizado em modo RGB
    """

    # ── Paleta de cores (extraída dos pixels do template original) ─────────────
    COR_VERMELHO = (255, 20, 31)   # fundo do badge "LANÇAMENTO"
    COR_AZUL     = (41, 45, 150)   # retângulo do código do produto
    COR_FUNDO_SC = (41, 45, 150)   # fundo escuro atrás do badge
    COR_DOURADO  = (255, 20, 31)   # borda dourada do badge
    COR_BRANCO   = (255, 255, 255)   # fundo das áreas limpas
    COR_TEXTO    = (0, 0, 0)   # cor principal do texto do card

    # ── Passo 1 — Criar canvas base e definir escala ─────────────────────────
    # Dimensões de referência fixas: 913×915 px.
    W, H      = 913, 915
    template  = Image.new("RGB", (W, H), (255, 255, 255))

    # Fatores de escala: coordenada_real = coordenada_ref × fator
    fator_x = W / 913
    fator_y = H / 915
    fator_s = min(fator_x, fator_y)  # fator único para fontes e raios

    def cx(valor): return int(valor * fator_x)   # escala horizontal
    def cy(valor): return int(valor * fator_y)   # escala vertical
    def cs(valor): return int(valor * fator_s)   # escala uniforme (fontes/raios)

    # ── Passo 2 — Definir início do rodapé ───────────────────────────────────
    RODAPE_Y_INICIO = int(0.9531 * H)            # ≈ y=872 numa imagem de 915px

    # ── Passo 3 — Trabalhar numa cópia do template ────────────────────────────
    canvas_base = template.copy()
    draw        = ImageDraw.Draw(canvas_base)

    # ══════════════════════════════════════════════════════════════════════════
    # ZONA 1 — BADGE (SELO VERMELHO)
    # Referência no template 913×915: x: 0–455, y: 0–122
    # Composto por: fundo escuro + borda dourada + pílula vermelha + texto
    # ══════════════════════════════════════════════════════════════════════════

    # Fundo escuro atrás do badge (cobre a metade esquerda do topo)
    draw.rectangle([0, 0, cx(380), cy(122)], fill=COR_FUNDO_SC)

    # Borda dourada ao redor do badge (2px maior que a pílula vermelha)
    CAIXA_DOURADA = [cx(37), cy(36), cx(455), cy(108)]
    retangulo_arredondado(draw, CAIXA_DOURADA, cs(18), COR_DOURADO)

    # Pílula vermelha (fundo do texto do badge)
    CAIXA_VERMELHA = [cx(40), cy(39), cx(452), cy(105)]
    retangulo_arredondado(draw, CAIXA_VERMELHA, cs(16), COR_VERMELHO)

    # Texto do badge — italic bold, auto-ajusta o tamanho para caber na pílula
    largura_badge = CAIXA_VERMELHA[2] - CAIXA_VERMELHA[0] - cs(24)
    fonte_badge   = fonte_auto_tamanho(draw, badge, FONTS_BOLD_ITALIC,
                                       largura_badge, tam_inicial=cs(50))
    centralizar_texto(draw, badge, fonte_badge, CAIXA_VERMELHA, COR_BRANCO)

    # ══════════════════════════════════════════════════════════════════════════
    # ZONA 2 — BARRA DE CÓDIGO + NOME DO PRODUTO
    # Referência no template 913×915: x: 7–906, y: 120–177
    # Esquerda: retângulo marrom com o código | Direita: fundo branco com o nome
    # ══════════════════════════════════════════════════════════════════════════

    # Barra de código: fonte fixa, largura e altura dinâmicas conforme o conteúdo
    fonte_codigo = carregar_fonte(FONTS_BOLD_ITALIC, cs(28))
    _PAD_COD   = cs(10)   # padding vertical interno
    _PAD_H_COD = cs(16)   # padding horizontal interno
    _SP_LINHAS = cs(5)    # espaço entre as duas linhas

    # Montar texto: "CI.: xxx | codigo_fabricante" ou só "codigo_fabricante"
    _tem_ci = bool(codigo_interno and codigo_interno.strip())
    _texto_codigo = (f"CI.: {codigo_interno.strip()} | {codigo}"
                     if _tem_ci else codigo)

    _bb_cod = draw.textbbox((0, 0), _texto_codigo, font=fonte_codigo)
    _lh_cod = _bb_cod[3] - _bb_cod[1]
    _lw_cod = _bb_cod[2] - _bb_cod[0]
    _barra_h = _lh_cod + 2 * _PAD_COD
    _barra_w = _lw_cod + 2 * _PAD_H_COD

    # Limitar para não ultrapassar metade do card
    _barra_w = max(cx(100), min(_barra_w, W // 2))

    CAIXA_CODIGO = [cx(7), cy(120), cx(7) + _barra_w, cy(120) + _barra_h]
    draw.rectangle(CAIXA_CODIGO, fill=COR_AZUL)

    centralizar_texto(draw, _texto_codigo, fonte_codigo, CAIXA_CODIGO, COR_BRANCO)

    # Área branca (lado direito) — mesma altura dinâmica da barra azul
    CAIXA_NOME = [CAIXA_CODIGO[2] + 1, CAIXA_CODIGO[1], W - cx(7), CAIXA_CODIGO[3]]
    draw.rectangle(CAIXA_NOME, fill=COR_BRANCO)

    # Pré-calcular layout do nome (será desenhado acima da foto na área principal)
    _NOME_FONTE  = carregar_fonte(FONTS_BOLD, cs(26))
    _NOME_LARG   = W - cx(40)
    _NOME_SP     = cs(6)
    _NOME_LINHAS = quebrar_linhas(draw, nome, _NOME_FONTE, _NOME_LARG)
    _NOME_LH     = draw.textbbox((0, 0), "Ag", font=_NOME_FONTE)[3]
    _NOME_H      = len(_NOME_LINHAS) * _NOME_LH + (len(_NOME_LINHAS) - 1) * _NOME_SP
    _NOME_PAD    = cs(14)   # margem acima e abaixo do texto

    # ══════════════════════════════════════════════════════════════════════════
    # ZONA 3 — ÁREA DO PRODUTO (foto central)
    # Referência no template 913×915: x: 10–903, y: 178–690
    # Quando uma foto é enviada, a área inteira é limpa com branco e a foto
    # é centralizada dentro de uma zona com margem interna.
    # ══════════════════════════════════════════════════════════════════════════

    # Limites da área reservada para a foto no template
    # AREA_Y0 segue o fundo da barra de código (dinâmico)
    AREA_X0 = cx(10);  AREA_Y0 = CAIXA_CODIGO[3] + 1
    AREA_X1 = cx(903); AREA_Y1 = cy(790)
    AREA_W  = AREA_X1 - AREA_X0   # largura total disponível
    AREA_H  = AREA_Y1 - AREA_Y0   # altura total disponível

    fim_foto  = AREA_Y1   # y final do conteúdo (padrão: sem foto)
    foto_ok   = False     # indica se a foto foi processada com sucesso

    if foto_produto is not None:
        try:
            # Abrir a foto e converter para RGBA (preserva transparência PNG)
            img_prod = Image.open(foto_produto).convert("RGBA")

            # Margem interna: evita que a foto encoste nas bordas da área
            MARGEM_X = cs(80)   # margem horizontal em cada lado (~80px na ref.)
            MARGEM_Y = cs(30)   # margem vertical em cada lado  (~30px na ref.)
            ZONA_W   = max(1, AREA_W - 2 * MARGEM_X)  # largura exibível
            ZONA_H   = max(1, AREA_H - 2 * MARGEM_Y)  # altura exibível

            # Calcular escala para que a foto caiba na zona (sobe E desce)
            # thumbnail() só reduz; aqui usamos resize() para também ampliar
            escala = min(ZONA_W / img_prod.width, ZONA_H / img_prod.height)
            novo_w = max(1, int(img_prod.width  * escala))
            novo_h = max(1, int(img_prod.height * escala))
            img_prod = img_prod.resize((novo_w, novo_h), Image.LANCZOS)

            # Calcular posição de colagem centralizada dentro da área
            col_x = AREA_X0 + (AREA_W - novo_w) // 2
            col_y = AREA_Y0 + (AREA_H - novo_h) // 2

            # Garantir que as coordenadas fiquem dentro dos limites da área
            col_x = max(AREA_X0, min(col_x, AREA_X1 - novo_w))
            col_y = max(AREA_Y0, min(col_y, AREA_Y1 - novo_h))

            fim_foto = col_y + novo_h   # ← posição Y do fim da foto
            foto_ok  = True

            # Limpar toda a faixa da área de produto até o início do rodapé
            draw.rectangle([0, AREA_Y0, W, RODAPE_Y_INICIO], fill=COR_BRANCO)

            # ── Desenhar nome acima da foto ───────────────────────────────────
            _ny = AREA_Y0 + _NOME_PAD
            for _linha in _NOME_LINHAS:
                _bb = draw.textbbox((0, 0), _linha, font=_NOME_FONTE)
                _lw = _bb[2] - _bb[0]
                _nx = AREA_X0 + (AREA_W - _lw) // 2
                draw.text((_nx, _ny - _bb[1]), _linha, font=_NOME_FONTE, fill=COR_TEXTO)
                _ny += _NOME_LH + _NOME_SP
            _NOME_FIM = AREA_Y0 + _NOME_PAD + _NOME_H + _NOME_PAD

            # Reposicionar foto para abaixo do bloco de nome
            _FOTO_H_DISP = max(1, AREA_Y1 - _NOME_FIM - cs(20))
            _FOTO_W_DISP = max(1, AREA_W - 2 * cs(60))
            _esc2    = min(_FOTO_W_DISP / img_prod.width, _FOTO_H_DISP / img_prod.height)
            novo_w   = max(1, int(img_prod.width  * _esc2))
            novo_h   = max(1, int(img_prod.height * _esc2))
            img_prod = img_prod.resize((novo_w, novo_h), Image.LANCZOS)
            col_x    = AREA_X0 + (AREA_W - novo_w) // 2
            col_y    = _NOME_FIM + (_FOTO_H_DISP - novo_h) // 2
            col_x    = max(AREA_X0, min(col_x, AREA_X1 - novo_w))
            col_y    = max(_NOME_FIM, min(col_y, AREA_Y1 - novo_h))
            fim_foto = col_y + novo_h

            # Colar a foto preservando transparência (alpha channel)
            base_rgba = canvas_base.convert("RGBA")
            base_rgba.paste(img_prod, (col_x, col_y), img_prod)
            canvas_base = base_rgba.convert("RGB")
            draw        = ImageDraw.Draw(canvas_base)

        except Exception:
            # Se falhar, mantém a área original do template (sem foto)
            foto_ok  = False
            fim_foto = AREA_Y1

    # Sem foto: desenhar nome no topo da área de produto
    if not foto_ok:
        draw.rectangle([AREA_X0, AREA_Y0, AREA_X1,
                        AREA_Y0 + _NOME_PAD + _NOME_H + _NOME_PAD], fill=COR_BRANCO)
        _ny = AREA_Y0 + _NOME_PAD
        for _linha in _NOME_LINHAS:
            _bb = draw.textbbox((0, 0), _linha, font=_NOME_FONTE)
            _lw = _bb[2] - _bb[0]
            _nx = AREA_X0 + (AREA_W - _lw) // 2
            draw.text((_nx, _ny - _bb[1]), _linha, font=_NOME_FONTE, fill=COR_TEXTO)
            _ny += _NOME_LH + _NOME_SP

    # ══════════════════════════════════════════════════════════════════════════
    # ZONA 3b — TEXTO "IMAGEM ILUSTRATIVA" VERTICAL (margem direita)
    # Sempre exibido, girado 90° anti-horário (para a esquerda).
    # ══════════════════════════════════════════════════════════════════════════

    fonte_ilustr = carregar_fonte(FONTS_BOLD, cs(22))
    TEXTO_ILUSTR = "IMAGEM ILUSTRATIVA"

    # Medir o texto
    _tmp_draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    bb_i = _tmp_draw.textbbox((0, 0), TEXTO_ILUSTR, font=fonte_ilustr)
    tw_i = bb_i[2] - bb_i[0]
    th_i = bb_i[3] - bb_i[1]

    # Criar canvas transparente com o texto horizontal
    txt_img  = Image.new("RGBA", (tw_i + cs(6), th_i + cs(6)), (255, 255, 255, 0))
    txt_draw = ImageDraw.Draw(txt_img)
    txt_draw.text((-bb_i[0] + cs(3), -bb_i[1] + cs(3)),
                  TEXTO_ILUSTR, font=fonte_ilustr, fill=(80, 80, 80, 255))

    # Rotacionar 90° anti-horário (para a esquerda)
    txt_rot = txt_img.rotate(90, expand=True)

    # Posição: margem direita da área, centralizado verticalmente
    rx = AREA_X1 - txt_rot.width - cs(6)
    ry = AREA_Y0 + (AREA_Y1 - AREA_Y0 - txt_rot.height) // 2
    ry = max(AREA_Y0, min(ry, AREA_Y1 - txt_rot.height))

    _base_r = canvas_base.convert("RGBA")
    _base_r.paste(txt_rot, (rx, ry), txt_rot)
    canvas_base = _base_r.convert("RGB")
    draw        = ImageDraw.Draw(canvas_base)

    # ══════════════════════════════════════════════════════════════════════════
    # ZONA 3c — LOGO DA MARCA (abaixo do retângulo azul de código)
    # Prioridade: marca_logo_bytes (BD Marcas) > imagem_marca (arquivo/legado)
    # Renderizado após a foto e limpeza da área, para não ser apagado.
    # ══════════════════════════════════════════════════════════════════════════
    _logo_marca_img = None

    if marca_logo_bytes is not None:
        try:
            _logo_marca_img = Image.open(io.BytesIO(marca_logo_bytes)).convert("RGBA")
        except Exception:
            pass
    elif imagem_marca and imagem_marca.strip():
        _nome_arq_marca = imagem_marca.strip()
        _base_logos     = _cfg("LOGOS_PATH")
        _caminho_marca  = (_nome_arq_marca
                           if os.path.exists(_nome_arq_marca)
                           else os.path.join(_base_logos, _nome_arq_marca))
        try:
            if os.path.exists(_caminho_marca):
                _logo_marca_img = Image.open(_caminho_marca).convert("RGBA")
        except Exception:
            pass

    if _logo_marca_img is not None:
        try:
            # Remove fundo branco/quase-branco para não bloquear a foto do produto
            _logo_marca_img = remover_fundo_branco(_logo_marca_img)

            # Retângulo delimitador do logo da marca (canto superior direito, ao lado do badge)
            # Referência 913×915: x: 460–905, y: 3–118
            _MARC_X0 = cx(460); _MARC_X1 = cx(905)
            _MARC_Y0 = cy(3);   _MARC_Y1 = cy(118)
            _MARC_W  = _MARC_X1 - _MARC_X0
            _MARC_H  = _MARC_Y1 - _MARC_Y0

            # Escala proporcional para caber no retângulo sem estourar
            _esc = min(_MARC_W / _logo_marca_img.width, _MARC_H / _logo_marca_img.height)
            _lw  = max(1, int(_logo_marca_img.width  * _esc))
            _lh  = max(1, int(_logo_marca_img.height * _esc))
            _logo_marca_img = _logo_marca_img.resize((_lw, _lh), Image.LANCZOS)

            # Centralizar dentro do retângulo
            _lx = _MARC_X0 + (_MARC_W - _lw) // 2
            _ly = _MARC_Y0 + (_MARC_H - _lh) // 2

            _base_m = canvas_base.convert("RGBA")
            _base_m.paste(_logo_marca_img, (_lx, _ly), _logo_marca_img)
            canvas_base = _base_m.convert("RGB")
            draw        = ImageDraw.Draw(canvas_base)
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════════════════════
    # ZONA 4 — VEÍCULOS COMPATÍVEIS
    # Posicionado dinamicamente: sempre logo abaixo do fim da foto.
    # Sem foto: usa a posição original do template.
    # Com foto: posição calculada a partir de fim_foto.
    # ══════════════════════════════════════════════════════════════════════════

    fonte_label  = carregar_fonte(FONTS_BOLD,    cs(20))   # "Veículos" — negrito
    fonte_valor  = carregar_fonte(FONTS_REGULAR, cs(20))   # valor — regular

    bb_label = draw.textbbox((0, 0), "Veículos", font=fonte_label)
    bb_valor = draw.textbbox((0, 0), veiculos,   font=fonte_valor)

    ALTURA_LABEL = bb_label[3] - bb_label[1]   # altura do texto "Veículos"
    ALTURA_VALOR = bb_valor[3] - bb_valor[1]   # altura do texto do valor
    ESPACO_LINHAS = cs(6)                       # espaço entre as duas linhas

    if foto_ok:
        # Com foto: posição dinâmica — 20px abaixo do fim da foto
        GAP_FOTO_VEI = cs(5)
        Y_VEI        = fim_foto + GAP_FOTO_VEI

        draw.text((cx(70), Y_VEI - bb_label[1]),
                  "Veículos", font=fonte_label, fill=COR_TEXTO)
        draw.text((cx(70), Y_VEI + ALTURA_LABEL + ESPACO_LINHAS - bb_valor[1]),
                  veiculos,   font=fonte_valor,  fill=COR_TEXTO)

        ALTURA_VEICULOS = ALTURA_LABEL + ESPACO_LINHAS + ALTURA_VALOR

    else:
        # Sem foto: apaga o texto original e redesenha na posição padrão
        draw.rectangle([cx(65), cy(695), cx(500), cy(795)], fill=COR_BRANCO)
        Y_VEI = cy(705)
        draw.text((cx(70), Y_VEI - bb_label[1]),
                  "Veículos", font=fonte_label, fill=COR_TEXTO)
        draw.text((cx(70), cy(733) - bb_valor[1]),
                  veiculos,   font=fonte_valor,  fill=COR_TEXTO)

        ALTURA_VEICULOS = cy(733) + ALTURA_VALOR - Y_VEI

    # ══════════════════════════════════════════════════════════════════════════
    # PASSO FINAL — MONTAR CANVAS COM ALTURA DINÂMICA
    #
    # Quando há foto, o canvas é recortado logo após o conteúdo (veículos) e
    # o rodapé é colado em seguida — eliminando o espaço em branco excedente.
    #
    # Sem foto, o template original já tem o rodapé na posição correta;
    # apenas retornamos o canvas_base sem redimensionar.
    # ══════════════════════════════════════════════════════════════════════════

    # ── Gerar rodapé personalizado (logo + contatos) ─────────────────────────
    rodape_custom = gerar_rodape(W, site, telefone, whatsapp, fator_s, endereco=endereco)
    RODAPE_CUSTOM_H = rodape_custom.height

    if foto_ok:
        # Calcular onde o conteúdo termina (veículos + margem inferior)
        MARGEM_RODAPE  = cs(20)            # espaço entre veículos e rodapé
        FIM_CONTEUDO   = Y_VEI + ALTURA_VEICULOS + MARGEM_RODAPE

        # Altura total do canvas: conteúdo + rodapé personalizado
        NOVA_ALTURA    = FIM_CONTEUDO + RODAPE_CUSTOM_H

        # Criar canvas em branco com a nova altura
        canvas_final = Image.new("RGB", (W, NOVA_ALTURA), COR_BRANCO)

        # Colar a porção processada do card (badge + código + foto + veículos)
        recorte_conteudo = canvas_base.crop((0, 0, W, min(FIM_CONTEUDO, H)))
        canvas_final.paste(recorte_conteudo, (0, 0))

        # Colar o rodapé personalizado logo abaixo do conteúdo
        canvas_final.paste(rodape_custom, (0, FIM_CONTEUDO))

        canvas_final._rodape_y   = FIM_CONTEUDO
        canvas_final._veiculos_y = Y_VEI
        canvas_final._foto_y     = col_y
        canvas_final._foto_fim   = fim_foto
        canvas_final._ilustr_x   = rx   # início da faixa "IMAGEM ILUSTRATIVA"
        return canvas_final

    else:
        # Sem foto: apagar o rodapé original do template e colar o personalizado
        NOVA_ALTURA  = RODAPE_Y_INICIO + RODAPE_CUSTOM_H
        canvas_final = Image.new("RGB", (W, NOVA_ALTURA), COR_BRANCO)
        recorte      = canvas_base.crop((0, 0, W, RODAPE_Y_INICIO))
        canvas_final.paste(recorte, (0, 0))
        canvas_final.paste(rodape_custom, (0, RODAPE_Y_INICIO))
        canvas_final._rodape_y   = RODAPE_Y_INICIO
        canvas_final._veiculos_y = Y_VEI
        canvas_final._foto_y     = AREA_Y0
        canvas_final._foto_fim   = AREA_Y0
        canvas_final._ilustr_x   = int(AREA_X1 - cs(28))
        return canvas_final


# ══════════════════════════════════════════════════════════════════════════════
# INTERFACE STREAMLIT
# ══════════════════════════════════════════════════════════════════════════════

def _processar_imagem_gemini(imagem_bytes: bytes, mime_type: str) -> bytes | None:
    """
    Envia a imagem para o Gemini para regeneração com qualidade profissional.
    Retorna os bytes da imagem gerada, ou None em caso de erro.
    """
    try:
        client = google_genai.Client(api_key=_cfg("GEMINI_API_KEY"))

        resposta = client.models.generate_content(
            model="gemini-3.1-flash-image",  # ou "gemini-3.1-flash-image"
            contents=[
                genai_types.Part.from_bytes(data=imagem_bytes, mime_type=mime_type),
                genai_types.Part(text=(
                    "Edit the image into a professional commercial studio product photo."

                    "Background:"
                    "- Replace the background with a pure white seamless studio background."
                    "- Keep the product centered."

                    "Lighting:"
                    "- Apply soft, diffused professional studio lighting."
                    "- Maintain realistic light behavior and natural reflections."
                    "- Add a soft, realistic contact shadow directly beneath the product."

                    "Preservation rules:"
                    "- Do not alter the product in any way."
                    "- Do not change shape, proportions, geometry, angle, perspective, scale, color, texture, material, finish, edges, stitching, prints, labels, logos, surface details, or imperfections."
                    "- Do not retouch, clean up, reconstruct, redesign, stylize, enhance, or reinterpret the product."
                    "- Do not generate missing details or improve the object."
                    "- Preserve every visible detail exactly as in the original image."

                    "Output requirements:"
                    "- Photorealistic result."
                    "- High sharpness and fine detail."
                    "- Clean premium e-commerce / catalog style."
                    "- The only allowed changes are background removal/replacement, lighting adjustment, and the addition of a realistic shadow."
                )),
            ],
            config=genai_types.GenerateContentConfig(
                response_modalities=["IMAGE", "TEXT"],
            ),
        )

        for part in resposta.candidates[0].content.parts:
            if part.inline_data and part.inline_data.data:
                return part.inline_data.data
        return None
    except Exception as e:
        st.error(f"Erro Gemini: {e}")
        return None


def _melhorar_logo_gemini(imagem_bytes: bytes, mime_type: str) -> bytes | None:
    """
    Melhora a logo usando PIL (sem IA generativa):
      1. Amplia para pelo menos 1 200 px no maior lado (LANCZOS)
      2. Aplica UnsharpMask para nitidez máxima
      3. Aumenta levemente contraste e saturação
      4. Remove fundo branco/quase-branco (torna transparente)
      5. Salva como PNG de alta qualidade

    Não usa Gemini para evitar deformação ou alteração do design original.
    """
    from PIL import ImageFilter, ImageEnhance

    try:
        img = Image.open(io.BytesIO(imagem_bytes)).convert("RGBA")

        # 1. Ampliar se a imagem for pequena (garante nitidez ao imprimir)
        TAMANHO_MIN = 1200
        maior = max(img.width, img.height)
        if maior < TAMANHO_MIN:
            escala = TAMANHO_MIN / maior
            novo_w = max(1, int(img.width  * escala))
            novo_h = max(1, int(img.height * escala))
            img = img.resize((novo_w, novo_h), Image.LANCZOS)

        # 2. Nitidez via UnsharpMask no canal RGB (preserva alpha)
        r, g, b, a = img.split()
        rgb = Image.merge("RGB", (r, g, b))
        rgb = rgb.filter(ImageFilter.UnsharpMask(radius=1.5, percent=180, threshold=2))

        # 3. Leve aumento de contraste e saturação
        rgb = ImageEnhance.Contrast(rgb).enhance(1.08)
        rgb = ImageEnhance.Color(rgb).enhance(1.05)

        # 4. Recompor RGBA e remover fundo branco
        r2, g2, b2 = rgb.split()
        img = Image.merge("RGBA", (r2, g2, b2, a))
        img = remover_fundo_branco(img, tolerancia=30)

        # 5. Salvar como PNG sem perda
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        buf.seek(0)
        return buf.read()

    except Exception as e:
        st.error(f"Erro ao processar logo: {e}")
        return None


def _redimensionar_e_comprimir(imagem_bytes: bytes,
                                kb_alvo: int = 350,
                                larg_max: int = 1800, alt_max: int = 1800,
                                margem: int = 100) -> bytes:
    """
    Redimensiona a imagem para caber em larg_max×alt_max e centraliza num
    canvas com margem em todos os lados. O canvas cresce conforme necessário.
    """
    img = Image.open(io.BytesIO(imagem_bytes)).convert("RGB")

    # Calcula escala preservando proporção (amplia E reduz)
    escala   = min(larg_max / img.width, alt_max / img.height)
    novo_w   = max(1, int(img.width  * escala))
    novo_h   = max(1, int(img.height * escala))
    img      = img.resize((novo_w, novo_h), Image.LANCZOS)

    canvas_w = img.width  + 2 * margem
    canvas_h = img.height + 2 * margem
    canvas   = Image.new("RGB", (canvas_w, canvas_h), (255, 255, 255))
    canvas.paste(img, (margem, margem))

    # Ajustar qualidade JPEG até atingir ≤ kb_alvo KB
    for qualidade in range(92, 20, -2):
        buf = io.BytesIO()
        canvas.save(buf, format="JPEG", quality=qualidade, optimize=True)
        if buf.tell() <= kb_alvo * 1024:
            break

    buf.seek(0)
    return buf.read()


def _montar_canvas_social(card_img, IMG_W, IMG_H):
    """
    Monta canvas para redes sociais com 4 fatias:
      1. Cabeçalho — fixo no topo
      2. Foto      — centralizada no espaço disponível (ampliada)
      3. Veículos  — logo acima do rodapé
      4. Rodapé    — no limite inferior
    """
    orig_w = card_img.width

    # Posições originais gravadas em gerar_card
    foto_y_orig    = getattr(card_img, '_foto_y',     int(0.45 * orig_w))
    foto_fim_orig  = getattr(card_img, '_foto_fim',   foto_y_orig)
    vei_y_orig     = getattr(card_img, '_veiculos_y', int(0.90 * orig_w))
    rodape_y_orig  = getattr(card_img, '_rodape_y',   int(0.9531 * card_img.height))

    escala = IMG_W / orig_w

    # ── Fatias no original ────────────────────────────────────────────────────
    cab_orig = card_img.crop((0, 0,             orig_w, foto_y_orig)).convert("RGB")
    fot_orig = card_img.crop((0, foto_y_orig,   orig_w, foto_fim_orig)).convert("RGB")
    vei_orig = card_img.crop((0, vei_y_orig,    orig_w, rodape_y_orig)).convert("RGB")
    rod_orig = card_img.crop((0, rodape_y_orig, orig_w, card_img.height)).convert("RGB")

    # ── Escalar rodapé para largura total; cabeçalho e veículos menores (mais
    #    espaço vertical para a foto)
    def _fit_w(img, w):
        h = max(1, int(img.height * (w / img.width)))
        return img.resize((w, h), Image.LANCZOS)

    cab    = _fit_w(cab_orig,  IMG_W)
    vei    = _fit_w(vei_orig,  IMG_W)
    rodape = _fit_w(rod_orig,  IMG_W)

    cab_h    = cab.height
    vei_h    = vei.height
    rodape_h = rodape.height

    # ── Espaço disponível para a foto ────────────────────────────────────────
    espaco_foto = IMG_H - cab_h - vei_h - rodape_h

    ilustr_x_orig = getattr(card_img, '_ilustr_x', int(fot_orig.width * 0.958))

    if fot_orig.width > 0 and fot_orig.height > 0 and espaco_foto > 10:
        # Escala: 1.5× a largura máxima ou preenche a altura — o menor dos dois
        escala_foto = min(espaco_foto / fot_orig.height, (IMG_W / fot_orig.width) * 1.5)
        foto_w = int(fot_orig.width  * escala_foto)
        foto_h = int(fot_orig.height * escala_foto)
        foto   = fot_orig.resize((foto_w, foto_h), Image.LANCZOS)
        foto_x = (IMG_W - foto_w) // 2   # centralizado
        foto_y = cab_h + (espaco_foto - foto_h) // 2

        # Faixa "IMAGEM ILUSTRATIVA": recortar do fot_orig e recolocar na borda direita
        ilustr_strip_orig = fot_orig.crop((ilustr_x_orig, 0, fot_orig.width, fot_orig.height))
        strip_w = max(1, int(ilustr_strip_orig.width  * escala_foto))
        strip_h = max(1, int(ilustr_strip_orig.height * escala_foto))
        ilustr_strip = ilustr_strip_orig.resize((strip_w, strip_h), Image.LANCZOS)
    else:
        foto         = None
        ilustr_strip = None

    # ── Montar canvas ─────────────────────────────────────────────────────────
    canvas = Image.new("RGB", (IMG_W, IMG_H), (255, 255, 255))
    canvas.paste(cab, (0, 0))
    if foto:
        canvas.paste(foto, (foto_x, foto_y))
        if ilustr_strip and foto_w > IMG_W:
            # Recolocar faixa do texto na borda direita do canvas
            canvas.paste(ilustr_strip, (IMG_W - strip_w, foto_y))
    canvas.paste(vei,    (0, IMG_H - rodape_h - vei_h))
    canvas.paste(rodape, (0, IMG_H - rodape_h))

    return canvas


@st.dialog("📸 Instagram", width="small")
def dialog_instagram(card_img):
    """Seleção de formato Instagram: Retrato ou Stories."""

    fmt = st.radio(
        "Formato",
        options=["retrato", "stories"],
        format_func=lambda x: "📐 Retrato  1080×1350  4:5" if x == "retrato"
                               else "📱 Stories  1080×1920  9:16",
        horizontal=True,
        label_visibility="collapsed",
    )

    if fmt == "retrato":
        IMG_W, IMG_H = 1080, 1350
        label   = "Retrato — 1080×1350 · 4:5"
        caption = "Prévia 1080×1350"
        fname   = "instagram_retrato"
        info    = "👉 Melhor para engajamento — ocupa mais espaço na tela"
    else:
        IMG_W, IMG_H = 1080, 1920
        label   = "Stories — 1080×1920 · 9:16"
        caption = "Prévia 1080×1920"
        fname   = "stories_vertical"
        info    = "⏱️ Até 15 s por story  ·  ⚠️ Evite texto no topo/rodapé"

    st.markdown(f"""
    <div style="background:linear-gradient(135deg,#833ab4,#fd1d1d,#fcb045);
                border-radius:10px;padding:12px 16px;margin:4px 0 12px;color:#fff;">
      <div style="font-size:1rem;font-weight:700;">{label}</div>
      <div style="font-size:.78rem;margin-top:4px;opacity:.9;">{info}</div>
    </div>
    """, unsafe_allow_html=True)

    canvas = _montar_canvas_social(card_img, IMG_W, IMG_H)
    st.image(canvas, width='stretch', caption=caption)

    buf_png = io.BytesIO()
    canvas.save(buf_png, format="PNG", dpi=(300, 300))
    buf_png.seek(0)
    buf_jpg = io.BytesIO()
    canvas.save(buf_jpg, format="JPEG", quality=95, dpi=(300, 300))
    buf_jpg.seek(0)

    col_a, col_b = st.columns(2)
    col_a.download_button("⬇ PNG", data=buf_png, file_name=f"{fname}.png",
                          mime="image/png",  width='stretch', key=f"dl_{fmt}_png")
    col_b.download_button("⬇ JPG", data=buf_jpg, file_name=f"{fname}.jpg",
                          mime="image/jpeg", width='stretch', key=f"dl_{fmt}_jpg")



@st.dialog("📤 Atualizar FTP", width="large")
def dialog_atualizar_ftp(codigo: str):
    import ftplib, io as _io

    # flags de sessão
    chave_enviado  = f"ftp_enviado_{codigo}"
    chave_original = f"ftp_original_{codigo}"
    chave_gemini   = f"ftp_gemini_{codigo}"
    chave_final    = f"ftp_final_{codigo}"
    chave_url      = f"ftp_url_{codigo}"
    chave_token    = f"ftp_token_{codigo}"
    chave_token_v  = f"ftp_token_visto_{codigo}"

    # Detecta nova abertura: token mudou → reseta todo o estado de imagem
    token_atual = st.session_state.get(chave_token, 0)
    if st.session_state.get(chave_token_v, -1) != token_atual:
        st.session_state[chave_token_v]  = token_atual
        st.session_state[chave_enviado]  = False
        st.session_state[chave_original] = None
        st.session_state[chave_gemini]   = None
        st.session_state[chave_final]    = None
        st.session_state[chave_url]      = ""
    else:
        for chave, padrao in [
            (chave_enviado,  False),
            (chave_original, None),
            (chave_gemini,   None),
            (chave_final,    None),
            (chave_url,      ""),
        ]:
            if chave not in st.session_state:
                st.session_state[chave] = padrao

    # ── Tela de sucesso (early return — única fonte de UI pós-envio) ─────────
    if st.session_state[chave_enviado]:
        nome_arquivo_dl = f"{codigo}_1.jpg"
        caminho_insert  = rf"D:\Imagens\{nome_arquivo_dl}"

        st.success("✅ Imagem enviada ao FTP com sucesso!")
        st.markdown("**Deseja salvar a imagem no seu computador?**")
        st.info(f"📁 Caminho: `{caminho_insert}`")
        st.download_button(
            "⬇️ Baixar imagem",
            data=st.session_state[chave_final],
            file_name=nome_arquivo_dl,
            mime="image/jpeg",
            use_container_width=True,
            key="ftp_dl",
        )

        st.divider()
        if st.button("❌ Fechar", use_container_width=True, key="ftp_fechar"):
            st.session_state[chave_enviado] = False
            st.rerun()
        return

    st.markdown(f"**Código:** `{codigo}`")
    st.info(f"O arquivo será salvo como `{codigo}_1.jpg` no servidor.")

    # ── Campo URL ────────────────────────────────────────────────────────────
    with st.form("ftp_url_form", clear_on_submit=False, border=False):
        url_digitada = st.text_input(
            "🔗 Cole a URL da imagem (pressione Enter para processar)",
            placeholder="https://exemplo.com/imagem.jpg",
            disabled=st.session_state[chave_enviado],
        )
        url_submetida = st.form_submit_button(
            "Carregar URL", use_container_width=True,
            disabled=st.session_state[chave_enviado],
        )

    if url_submetida and url_digitada and url_digitada != st.session_state[chave_url]:
        _HEADERS_NAVEGADOR = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "Referer": "/".join(url_digitada.split("/")[:3]) + "/",
        }
        try:
            with st.spinner("⬇️ Baixando imagem da URL..."):
                resp = requests.get(url_digitada, timeout=20, headers=_HEADERS_NAVEGADOR)
                resp.raise_for_status()
                imagem_bytes_url = resp.content

            if len(imagem_bytes_url) < 1024:
                st.error("URL não retornou uma imagem válida.")
                st.session_state[chave_original] = None
                st.session_state[chave_gemini]   = None
                st.session_state[chave_final]    = None
            else:
                ext_url  = url_digitada.lower().split("?")[0].split(".")[-1]
                mime_map = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}
                mime_url = mime_map.get(ext_url, "image/jpeg")

                st.session_state[chave_url]      = url_digitada
                st.session_state[chave_original] = imagem_bytes_url
                with st.spinner("🤖 Processando com Gemini (realismo + sombra)..."):
                    resultado_gemini_url = _processar_imagem_gemini(imagem_bytes_url, mime_url)
                st.session_state[chave_gemini] = resultado_gemini_url if resultado_gemini_url else imagem_bytes_url
                if not resultado_gemini_url:
                    st.warning("Gemini não retornou imagem. Usando imagem original para redimensionamento.")
                st.session_state[chave_final] = None
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            if status in (403, 401):
                st.error(
                    f"**{status} — acesso bloqueado pelo servidor da imagem.**\n\n"
                    "Sites como Canva, Getty e similares impedem o download direto por URL. "
                    "Salve a imagem no seu computador e use o campo de **upload de arquivo** abaixo."
                )
            else:
                st.error(f"Erro HTTP {status} ao baixar a URL.")
            st.session_state[chave_original] = None
            st.session_state[chave_gemini]   = None
            st.session_state[chave_final]    = None
        except Exception as e:
            st.error(f"Erro ao baixar URL: {e}")
            st.session_state[chave_original] = None
            st.session_state[chave_gemini]   = None
            st.session_state[chave_final]    = None

    st.markdown("---")

    arquivo = st.file_uploader(
        "Ou selecione um arquivo (JPG ou PNG)",
        type=["jpg", "jpeg", "png"],
        key="ftp_upload",
        disabled=st.session_state[chave_enviado],
    )

    # Ao selecionar novo arquivo, processa com Gemini e guarda em session_state
    if arquivo and not st.session_state[chave_enviado]:
        imagem_bytes = arquivo.getvalue()
        mime_type    = arquivo.type or "image/jpeg"

        if st.session_state[chave_original] != imagem_bytes:
            st.session_state[chave_original] = imagem_bytes
            st.session_state[chave_url]      = ""  # limpa URL ao trocar para upload
            with st.spinner("🤖 Processando com Gemini (realismo + sombra)..."):
                resultado_gemini = _processar_imagem_gemini(imagem_bytes, mime_type)
            st.session_state[chave_gemini] = resultado_gemini if resultado_gemini else imagem_bytes
            if not resultado_gemini:
                st.warning("Gemini não retornou imagem. Usando imagem original para redimensionamento.")
            st.session_state[chave_final] = None  # força re-render do tamanho

    imagem_final = st.session_state[chave_final]

    if st.session_state[chave_gemini] and not st.session_state[chave_enviado]:
        st.markdown("---")

        # 300 DPI → 1 cm = 300/2.54 ≈ 118.11 px
        PX_POR_CM = 300 / 2.54
        MARGEM_PX = 100

        st.markdown(
            f"**⚙️ Ajuste de tamanho** — 300 DPI · "
            f"margem automática de {round(MARGEM_PX/PX_POR_CM,1)} cm em cada lado"
        )

        col_w, col_h, col_btn = st.columns([2, 2, 1])
        with col_w:
            larg_cm = st.number_input(
                "Largura (cm)", min_value=1.0,
                value=15.2, step=0.1, format="%.1f", key="ftp_largura_cm",
            )
        with col_h:
            alt_cm = st.number_input(
                "Altura (cm)", min_value=1.0,
                value=15.2, step=0.1, format="%.1f", key="ftp_altura_cm",
            )
        with col_btn:
            st.markdown("<div style='margin-top:28px'></div>", unsafe_allow_html=True)
            atualizar = st.button("🔄 Aplicar", width='stretch')

        # Converte cm → px (sem limite máximo)
        larg_px = int(larg_cm * PX_POR_CM)
        alt_px  = int(alt_cm  * PX_POR_CM)

        canvas_w = larg_px + 2 * MARGEM_PX
        canvas_h = alt_px  + 2 * MARGEM_PX
        st.caption(
            f"Canvas resultante: **{round(canvas_w/PX_POR_CM,1)} × {round(canvas_h/PX_POR_CM,1)} cm** "
            f"({canvas_w} × {canvas_h} px)"
        )

        # Aplica automaticamente na primeira vez ou ao clicar Aplicar
        if atualizar or imagem_final is None:
            with st.spinner(f"📐 Redimensionando para {larg_cm:.1f}×{alt_cm:.1f} cm..."):
                imagem_final = _redimensionar_e_comprimir(
                    st.session_state[chave_gemini],
                    kb_alvo=350,
                    larg_max=larg_px,
                    alt_max=alt_px,
                    margem=MARGEM_PX,
                )
            st.session_state[chave_final] = imagem_final

        col_orig, col_proc = st.columns(2)
        with col_orig:
            st.markdown("**Original**")
            st.image(st.session_state[chave_original], width="stretch")
        with col_proc:
            st.markdown(
                f"**Processada — {len(imagem_final)//1024} KB · "
                f"{larg_cm:.1f}×{alt_cm:.1f} cm**"
            )
            st.image(imagem_final, width="stretch")

    col1, col2 = st.columns(2)
    enviar   = col1.button("✅ Enviar",   width='stretch', disabled=imagem_final is None)
    cancelar = col2.button("❌ Cancelar", width='stretch')

    if cancelar:
        st.rerun()

    if enviar:
        ftp_host     = _cfg("FTP_HOST")
        ftp_port     = int(_cfg("FTP_PORT") or 21)
        ftp_user     = _cfg("FTP_USER")
        ftp_password = _cfg("FTP_PASSWORD")
        ftp_pasta    = _cfg("FTP_PASTA") or "/imagens"
        nome_arquivo = f"{codigo}_1.jpg"

        if not ftp_host or not ftp_user:
            st.error("Credenciais FTP não configuradas. Verifique as variáveis FTP_HOST, FTP_USER e FTP_PASSWORD.")
        else:
            # ── Envio FTP ────────────────────────────────────────────────
            ftp_ok = False
            try:
                with st.spinner("📡 Conectando ao FTP e enviando..."):
                    ftp = ftplib.FTP()
                    ftp.connect(ftp_host, ftp_port, timeout=30)
                    ftp.login(ftp_user, ftp_password)
                    try:
                        ftp.cwd(ftp_pasta)
                    except ftplib.error_perm:
                        ftp.mkd(ftp_pasta)
                        ftp.cwd(ftp_pasta)
                    ftp.storbinary(f"STOR {nome_arquivo}", _io.BytesIO(imagem_final))
                    ftp.quit()
                ftp_ok = True
            except Exception as e:
                st.error(f"Erro FTP: {e}")

            # ── Salva cópia local em D:\imagens\ (somente Windows) ──────
            if ftp_ok:
                if os.name == "nt":
                    pasta_local = r"D:\imagens"
                    try:
                        os.makedirs(pasta_local, exist_ok=True)
                        caminho_local = os.path.join(pasta_local, nome_arquivo)
                        with open(caminho_local, "wb") as f_local:
                            f_local.write(imagem_final)
                    except Exception as e_local:
                        st.warning(f"⚠️ FTP enviado, mas falhou ao salvar localmente em {pasta_local}: {e_local}")

                st.session_state[chave_enviado] = True
                st.session_state[chave_final]   = imagem_final
                st.rerun(scope="fragment")


@st.dialog("➕ Cadastrar Marca", width="large")
def dialog_cadastrar_marca():
    """
    Dialog para cadastrar uma nova marca com logo melhorada pelo Gemini.

    Fluxo:
        1. Usuário digita o nome da marca
        2. Faz upload da logo (ou cola URL)
        3. Gemini processa: remove fundo + melhora qualidade
        4. Exibe comparação original × melhorada
        5. Botão Salvar grava no banco (tabela Marcas)
    """
    # ── Campo para digitar o nome da marca ──────────────────────────────────
    _raw_marca = st.text_input(
        "✏️ Nome da marca (somente letras)",
        value="",
        placeholder="Ex: MERCEDES, VOLVO, SCANIA…",
        key="dlg_nova_marca_nome",
    )

    # Permitir somente letras e espaços, converter para maiúsculo
    nome_marca = re.sub(r"[^A-Za-zÀ-ÿ\s]", "", _raw_marca).upper().strip()

    if nome_marca != _raw_marca.strip() and _raw_marca.strip():
        st.caption(f"🔤 Nome ajustado: **{nome_marca}**")

    if not nome_marca:
        st.info("Digite o nome da marca para continuar.")
        if st.button("❌ Cancelar", width='stretch'):
            st.rerun()
        return

    chave_orig  = f"mrc_orig_{nome_marca}"
    chave_gem   = f"mrc_gem_{nome_marca}"
    chave_salvo = f"mrc_salvo_{nome_marca}"
    chave_url   = f"mrc_url_{nome_marca}"

    for chave, padrao in [
        (chave_orig,  None),
        (chave_gem,   None),
        (chave_salvo, False),
        (chave_url,   ""),
    ]:
        if chave not in st.session_state:
            st.session_state[chave] = padrao

    def _limpar_dialog():
        for ch in (chave_orig, chave_gem, chave_url):
            st.session_state[ch] = None if ch != chave_url else ""
        st.session_state[chave_salvo] = False

    # ── Tela de confirmação pós-salvamento ─────────────────────────────────
    if st.session_state[chave_salvo]:
        st.success(f"✅ Marca **{nome_marca}** cadastrada com sucesso!")
        if st.button("Fechar", width='stretch'):
            st.session_state[chave_salvo] = False
            st.rerun()
        return

    st.markdown("---")
    st.info("Cole a URL da imagem **ou** faça upload. O Gemini melhora automaticamente.")

    # ── Opção 1: URL da imagem ──────────────────────────────────────────────
    chave_url = f"mrc_url_{nome_marca}"
    if chave_url not in st.session_state:
        st.session_state[chave_url] = ""

    url_logo = st.text_input(
        "🔗 URL da imagem",
        value=st.session_state[chave_url],
        placeholder="https://exemplo.com/logo.png",
        key=f"mrc_url_input_{nome_marca}",
    )

    if url_logo.strip() and url_logo.strip() != st.session_state[chave_url]:
        st.session_state[chave_url] = url_logo.strip()
        with st.spinner("⬇ Baixando imagem da URL..."):
            try:
                _headers = {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0 Safari/537.36"
                    ),
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
                    "Referer": url_logo.strip(),
                }
                resp = requests.get(url_logo.strip(), headers=_headers, timeout=10)
                resp.raise_for_status()
                imagem_bytes = resp.content
                # detectar mime pelo Content-Type ou extensão
                ct = resp.headers.get("Content-Type", "image/png").split(";")[0].strip()

                # Converter SVG para PNG automaticamente
                _is_svg = (
                    "svg" in ct.lower()
                    or url_logo.strip().lower().endswith(".svg")
                    or imagem_bytes[:256].lstrip().startswith((b"<svg", b"<?xml"))
                )
                if _is_svg:
                    with st.spinner("🔄 Convertendo SVG para PNG..."):
                        imagem_bytes = _converter_svg_para_png(imagem_bytes)
                    ct = "image/png"

                if ct not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
                    ct = "image/png"
                if st.session_state[chave_orig] != imagem_bytes:
                    st.session_state[chave_orig] = imagem_bytes
                    st.session_state[chave_gem]  = None
                    with st.spinner("🤖 Melhorando logo com Gemini..."):
                        resultado = _melhorar_logo_gemini(imagem_bytes, ct)
                    st.session_state[chave_gem] = resultado if resultado else imagem_bytes
                    if not resultado:
                        st.warning("Gemini não retornou imagem. Usando original.")
            except Exception as e:
                st.error(f"Erro ao baixar a imagem: {e}")

    st.markdown("<div style='text-align:center;color:#888;font-size:.8rem;margin:4px 0'>— ou —</div>",
                unsafe_allow_html=True)

    # ── Opção 2: Upload de arquivo ──────────────────────────────────────────
    arquivo = st.file_uploader(
        "📂 Upload da logo (JPG, PNG, WebP ou SVG)",
        type=["jpg", "jpeg", "png", "webp", "svg"],
        key=f"mrc_upload_{nome_marca}",
    )

    # ── Processar arquivo enviado via upload ────────────────────────────────
    if arquivo:
        imagem_bytes = arquivo.getvalue()
        mime_type    = arquivo.type or "image/png"

        # Converter SVG para PNG automaticamente
        _nome_arq = (arquivo.name or "").lower()
        if (
            "svg" in mime_type.lower()
            or _nome_arq.endswith(".svg")
            or imagem_bytes[:256].lstrip().startswith((b"<svg", b"<?xml"))
        ):
            with st.spinner("🔄 Convertendo SVG para PNG..."):
                imagem_bytes = _converter_svg_para_png(imagem_bytes)
            mime_type = "image/png"

        if st.session_state[chave_orig] != imagem_bytes:
            st.session_state[chave_orig] = imagem_bytes
            st.session_state[chave_gem]  = None
            with st.spinner("🤖 Melhorando logo com Gemini (fundo branco + qualidade)..."):
                resultado = _melhorar_logo_gemini(imagem_bytes, mime_type)
            if resultado:
                st.session_state[chave_gem] = resultado
            else:
                st.warning("Gemini não retornou imagem. Será usada a imagem original.")
                st.session_state[chave_gem] = imagem_bytes

    # ── Exibir prévia e botões ─────────────────────────────────────────────
    if st.session_state[chave_gem]:
        col_orig, col_proc = st.columns(2)
        with col_orig:
            st.markdown("**Original**")
            st.image(st.session_state[chave_orig], width='stretch')
        with col_proc:
            st.markdown("**Melhorada pelo Gemini**")
            st.image(st.session_state[chave_gem], width='stretch')

        st.markdown("---")
        col1, col2 = st.columns(2)
        salvar   = col1.button("💾 Salvar Marca", width='stretch')
        cancelar = col2.button("❌ Cancelar",     width='stretch')

        if cancelar:
            _limpar_dialog()
            st.rerun()

        if salvar:
            with st.spinner("Salvando no banco de dados..."):
                ok = cadastrar_marca(
                    nome=nome_marca,
                    logo_dados=st.session_state[chave_gem],
                    logo_mime="image/png",
                )
            if ok:
                st.session_state[chave_salvo] = True
                st.rerun()
    else:
        if st.button("❌ Cancelar", width='stretch'):
            _limpar_dialog()
            st.rerun()


st.set_page_config(
    page_title="Gerador de Promoção — Dinatec",
    page_icon="🔧",
    layout="wide",
)

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Poppins:wght@400;600;700&display=swap');
*, body { font-family: 'Poppins', sans-serif; }

.titulo {
    font-size: 1.9rem; font-weight: 700; color: #3b1f00;
    border-left: 6px solid #f01112; padding-left: 14px; margin-bottom: 3px;
}
.sub { color: #7a5c2e; font-size: .87rem; margin-bottom: 1.4rem; }

section[data-testid="stSidebar"] {
    background: #fdf6ec;
    border-right: 2px solid #d4b07a;
}
section[data-testid="stSidebar"] label {
    font-weight: 600 !important;
    color: #3b1f00 !important;
}

div[data-testid="stDownloadButton"] > button {
    background: linear-gradient(135deg, #2d1a00, #3b1f00);
    color: white; font-weight: 700;
    border: none; border-radius: 10px;
    padding: 13px 0; width: 100%;
    font-size: .95rem; letter-spacing: .4px;
    box-shadow: 0 3px 8px rgba(0,0,0,.25);
    transition: all .2s ease;
}
div[data-testid="stDownloadButton"] > button:hover {
    background: linear-gradient(135deg, #5a3200, #7a4500);
    box-shadow: 0 5px 14px rgba(0,0,0,.35);
    transform: translateY(-1px);
}
</style>
""", unsafe_allow_html=True)

# ── Dados das unidades da empresa ─────────────────────────────────────────────
_EMPRESAS = {
    "Selecione a unidade...": {
        "endereco": "", "site": "", "telefone": "", "whatsapp": "",
    },
    "Matriz - Ribeirão Preto": {
        "endereco": "Av. Luiz Maggioni, 1585 - Dist. Industrial Pref. Luiz Roberto Jabali\nRibeirão Preto - SP - CEP 14072-055",
        "site":     "www.dinatec.com.br",
        "telefone": "(16) 2111 - 9100",
        "whatsapp": "(16) 99631 - 7999",
    },
    "Filial - Araraquara": {
        "endereco": "Av. Pres. Vargas, 2644 - Jardim Quitandinha, Araraquara - SP, 14801-018",
        "site": "www.dinatec.com.br", 
        "telefone": "(16) 3301 - 0110", 
        "whatsapp": "(16) 99631 - 7999",
    },
    "Filial - São José do Rio Preto": {
        "endereco": "R. Dr. Coutinho Cavalcante, 1310 - Jardim America, São José do Rio Preto - SP, 15055-300", 
        "site": "www.dinatec.com.br", 
        "telefone": "(17) 2138 - 1892", 
        "whatsapp": "(16) 99631 - 7999",
    },
    "Filial - Limeira": {
        "endereco": "R. Doná Geni Vargas Machado Gomes, 375 - Jardim Residencial, Limeira - SP, 13485-213", 
        "site": "www.dinatec.com.br", 
        "telefone": "(19) 3444 - 2001", 
        "whatsapp": "(16) 99631 - 7999",
    },
    "Filial - Itumbiara": {
        "endereco": "Av. Dr. Celso Maeda, 2850 - A - Jardim Liberdade, Itumbiara - GO, 75515-255", 
        "site": "www.dinatec.com.br", 
        "telefone": "(64) 3048 - 2816", 
        "whatsapp": "(16) 99631 - 7999",
    },
    "Filial - Brasília": {
        "endereco": "St. G Sul Q CS CSG 5 - Taguatinga, Brasília - DF, 72035-505", 
        "site": "www.dinatec.com.br", 
        "telefone": "(61) 3356 - 0046", 
        "whatsapp": "(16) 99631 - 7999",
    },
}

st.markdown('<div class="titulo">Gerador de Postagem Grupo Dinatec</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="sub">Dinatec · Edite os campos · A prévia atualiza em tempo real</div>',
    unsafe_allow_html=True
)

# ── Painel lateral — campos editáveis e upload da foto ────────────────────────
with st.sidebar:
    def campo_obrigatorio(label, **kwargs):
        valor = st.text_input(label + " *", **kwargs)
        if not valor.strip():
            st.caption("⚠️ Campo obrigatório")
        return valor

    st.markdown("### ✏️ Textos do Card")
    badge = st.selectbox("🏷️ Selo (badge vermelho) *",
                         options=["", "LANÇAMENTO", "PROMOÇÃO", "QUEIMA DE ESTOQUE"])
    if not badge:
        st.caption("⚠️ Campo obrigatório")

    codigo_interno = st.text_input("🔍 Código Interno do Produto", value="",
                                   placeholder="Ex: 80741")

    # Auto-preenchimento ao buscar no banco
    _cod_pre      = ""
    _nome_pre     = ""
    _imagem_marca = ""
    _imagens      = []
    if codigo_interno.strip():
        with st.spinner("Consultando banco..."):
            resultado = buscar_produto(codigo_interno.strip())
        if resultado is None:
            st.warning("⚠️ Banco indisponível ou produto não encontrado. Preencha manualmente.")
        elif resultado == {}:
            st.warning("⚠️ Produto não encontrado.")
        else:
            _cod_pre      = resultado["num_fabricante"]
            _nome_pre     = resultado["descricao"]
            _imagem_marca = resultado["imagem_marca"]

        with st.spinner("Buscando imagens..."):
            _imagens = buscar_imagens_produto(codigo_interno.strip())

    codigo   = campo_obrigatorio("🔢 Numero do Fabricante", value=_cod_pre)
    nome     = campo_obrigatorio("📦 Descrição do produto", value=_nome_pre)
    veiculos = campo_obrigatorio("🚛 Veículos compatíveis", value="")

    # ── Campo Marca do Produto ─────────────────────────────────────────────
    st.markdown("### 🏭 Marca do Produto")

    # Buscar todas as marcas cadastradas para popular o combobox
    _marcas_cadastradas = listar_marcas()

    _OPCAO_SELECIONE  = "Selecione a marca..."
    _OPCAO_CADASTRAR  = "➕ Cadastrar nova marca..."
    _opcoes_marca = [_OPCAO_SELECIONE] + _marcas_cadastradas + [_OPCAO_CADASTRAR]

    # Se o banco retornou uma marca via código interno, pré-selecionar
    _idx_marca = 0
    if _imagem_marca and _imagem_marca.strip():
        # Tenta encontrar a marca retornada pelo BD na lista
        _marca_bd_nome = _imagem_marca.strip()
        for _i, _op in enumerate(_opcoes_marca):
            if _op.lower() == _marca_bd_nome.lower():
                _idx_marca = _i
                break

    marca_selecionada = st.selectbox(
        "🏷️ Marca do Produto",
        options=_opcoes_marca,
        index=_idx_marca,
        key="campo_marca",
    )

    _marca_logo_bytes: bytes | None = None

    if marca_selecionada == _OPCAO_CADASTRAR:
        # Abre o dialog diretamente ao selecionar a opção
        dialog_cadastrar_marca()

    elif marca_selecionada and marca_selecionada != _OPCAO_SELECIONE:
        # Marca existente selecionada — buscar logo do banco
        with st.spinner("Buscando marca no banco..."):
            _resultado_marca = buscar_marca(marca_selecionada)

        if _resultado_marca is None:
            st.warning("⚠️ Banco indisponível. Verifique a conexão.")
        elif _resultado_marca == {}:
            st.warning(f"⚠️ Marca **{marca_selecionada}** não encontrada no banco.")
        else:
            st.success(f"✅ Marca **{_resultado_marca['nome']}** encontrada!")
            _marca_logo_bytes = _resultado_marca.get("logo_dados")
            if _marca_logo_bytes:
                st.image(
                    _marca_logo_bytes,
                    caption="Logo da marca",
                    width=160,
                )

    st.markdown("### 🏢 Unidade / Contatos")

    empresa_sel = st.selectbox(
        "🏬 Selecione a unidade",
        options=list(_EMPRESAS.keys()),
        key="empresa_sel",
    )
    _emp = _EMPRESAS[empresa_sel]

    if _emp["endereco"]:
        st.markdown(
            f"""<div style="background:#f0f7ff;border-left:4px solid #2979c0;
                            border-radius:6px;padding:8px 12px;margin:6px 0 10px;
                            font-size:.82rem;color:#1a3a5c;line-height:1.6;">
                📍 {_emp['endereco'].replace(chr(10),'<br>')}
                </div>""",
            unsafe_allow_html=True,
        )

    _key = empresa_sel  # força reset dos campos ao trocar de empresa
    site      = campo_obrigatorio("🌍 Site",      value=_emp["site"],      placeholder="www.dinatec.com.br", key=f"site_{_key}")
    telefone  = campo_obrigatorio("📞 Telefone",  value=_emp["telefone"],  placeholder="(xx) xxxx-xxxx",     key=f"tel_{_key}")
    whatsapp  = campo_obrigatorio("💬 WhatsApp",  value=_emp["whatsapp"],  placeholder="(xx) xxxxx-xxxx",    key=f"wp_{_key}")
    endereco  = _emp["endereco"]

    st.markdown("### 📦 Foto do Produto")

    foto_upload  = None
    _foto_bytes  = None

    if _imagens:
        opcoes = [img["label"] for img in _imagens]
        escolha = st.selectbox("🖼️ Imagens encontradas no site", opcoes)
        idx = opcoes.index(escolha)
        _foto_bytes = _imagens[idx]["bytes"]
        st.image(_imagens[idx]["url"], caption=escolha, width="content")
        _codigo_ftp = codigo_interno.strip()
        if st.button("📤 Atualizar imagem", width='stretch', key="btn_atualizar_img"):
            import time as _time
            st.session_state[f"ftp_token_{_codigo_ftp}"] = _time.time()
            dialog_atualizar_ftp(_codigo_ftp)
    elif codigo_interno.strip():
        st.warning(
            "⚠️ Nenhuma imagem encontrada no site para este código.\n\n"
            "Faça o carregamento manual abaixo."
        )
        _codigo_ftp = codigo_interno.strip()
        if st.button("📤 Atualizar FTP", width='stretch'):
            import time as _time
            st.session_state[f"ftp_token_{_codigo_ftp}"] = _time.time()
            dialog_atualizar_ftp(_codigo_ftp)

    foto_upload = st.file_uploader(
        "Envie a foto manualmente (PNG ou JPG)",
        type=["png", "jpg", "jpeg", "webp"],
        help=(
            "A foto será centralizada na área do meio do card.\n"
            "Melhor resultado: fundo branco ou transparente (PNG)."
        ),
    )

    # Upload manual tem prioridade sobre a imagem do site
    if foto_upload:
        _foto_bytes = foto_upload.read()

    foto_final = io.BytesIO(_foto_bytes) if _foto_bytes else None

# ── Gerar e exibir o card ─────────────────────────────────────────────────────
col_preview, col_download = st.columns([3, 1], gap="large")

faltando = [v for v in [badge, codigo, nome, veiculos, site, telefone, whatsapp]
            if not v.strip()]

if faltando:
    st.stop()

try:
    card = gerar_card(badge, codigo, nome, veiculos, foto_final, site, telefone, whatsapp,
                      imagem_marca=_imagem_marca,
                      marca_logo_bytes=_marca_logo_bytes,
                      endereco=endereco,
                      codigo_interno=codigo_interno)

    with col_preview:
        st.image(card, width='stretch',
                 caption="Prévia — atualiza automaticamente ao editar os campos")

    with col_download:
        # Gerar buffers
        buf_png = io.BytesIO()
        card.save(buf_png, format="PNG", dpi=(600, 600))
        buf_png.seek(0)
        kb_png = len(buf_png.getvalue()) // 1024

        buf_jpg = io.BytesIO()
        card.convert("RGB").save(buf_jpg, format="JPEG", quality=100, dpi=(600, 600))
        buf_jpg.seek(0)
        kb_jpg = len(buf_jpg.getvalue()) // 1024

        st.markdown(f"""
        <div style="background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);
                    border-radius:10px;padding:12px 14px;margin-bottom:8px;">
          <div style="color:#f5c87a;font-size:.8rem;font-weight:700;
                      text-transform:uppercase;letter-spacing:.8px;">
            PNG — Sem perda
          </div>
          <div style="color:#ccc;font-size:.75rem;margin-top:2px;">
            {card.width}×{card.height} px &nbsp;·&nbsp; {kb_png} KB
          </div>
        </div>
        """, unsafe_allow_html=True)
        st.download_button(
            "⬇ Baixar PNG",
            data=buf_png,
            file_name=f"promo_{_nome_arquivo(codigo)}.png",
            mime="image/png",
            width='stretch',
            key="dl_png",
        )

        st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)

        st.markdown(f"""
        <div style="background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);
                    border-radius:10px;padding:12px 14px;margin-bottom:8px;">
          <div style="color:#f5c87a;font-size:.8rem;font-weight:700;
                      text-transform:uppercase;letter-spacing:.8px;">
            JPG — Comprimido
          </div>
          <div style="color:#ccc;font-size:.75rem;margin-top:2px;">
            {card.width}×{card.height} px &nbsp;·&nbsp; {kb_jpg} KB
          </div>
        </div>
        """, unsafe_allow_html=True)
        st.download_button(
            "⬇ Baixar JPG",
            data=buf_jpg,
            file_name=f"promo_{_nome_arquivo(codigo)}.jpg",
            mime="image/jpeg",
            width='stretch',
            key="dl_jpg",
        )

        st.markdown("<div style='height:10px'></div>", unsafe_allow_html=True)

        st.markdown("""
        <div style="background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);
                    border-radius:10px;padding:12px 14px;margin-bottom:8px;">
          <div style="color:#f5c87a;font-size:.8rem;font-weight:700;
                      text-transform:uppercase;letter-spacing:.8px;">
            Instagram — Retrato
          </div>
          <div style="color:#ccc;font-size:.75rem;margin-top:2px;">
            1080×1350 px &nbsp;·&nbsp; 4:5
          </div>
        </div>
        """, unsafe_allow_html=True)
        if st.button("📸 Instagram", width='stretch', key="btn_instagram"):
            dialog_instagram(card)

        st.markdown("---")

        # ── Gerar Unidade: ZIP com um JPG por unidade ─────────────────────────
        _unidades = {k: v for k, v in _EMPRESAS.items()
                     if k != "Selecione a unidade..."}
        _buf_zip = io.BytesIO()
        with zipfile.ZipFile(_buf_zip, "w", zipfile.ZIP_DEFLATED) as _zf:
            for _nome_un, _dados_un in _unidades.items():
                _foto_un = io.BytesIO(_foto_bytes) if _foto_bytes else None
                _card_un = gerar_card(
                    badge, codigo, nome, veiculos, _foto_un,
                    site=_dados_un["site"],
                    telefone=_dados_un["telefone"],
                    whatsapp=_dados_un["whatsapp"],
                    imagem_marca=_imagem_marca,
                    marca_logo_bytes=_marca_logo_bytes,
                    endereco=_dados_un["endereco"],
                    codigo_interno=codigo_interno,
                )
                _buf_un = io.BytesIO()
                _card_un.save(_buf_un, format="JPEG", quality=92)
                _arq_un = re.sub(r"[^\w]", "_", _nome_un).strip("_")
                _zf.writestr(
                    f"promo_{_nome_arquivo(codigo)}_{_arq_un}.jpg",
                    _buf_un.getvalue(),
                )
        _buf_zip.seek(0)

        st.download_button(
            "📦 Gerar Unidade",
            data=_buf_zip,
            file_name=f"promo_{_nome_arquivo(codigo)}_unidades.zip",
            mime="application/zip",
            width='stretch',
            key="dl_zip",
        )

except Exception as erro:
    with col_preview: 
        st.error(f"Erro ao gerar o card:\n\n`{erro}`")
