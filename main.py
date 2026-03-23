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
import pymssql
import requests
from dotenv import load_dotenv

load_dotenv()


def _cfg(chave: str) -> str:
    """Lê configuração: tenta st.secrets primeiro, depois variável de ambiente."""
    try:
        return st.secrets[chave]
    except Exception:
        return os.getenv(chave, "")


# ──────────────────────────────────────────────────────────────────────────────
# BANCO DE DADOS
# ──────────────────────────────────────────────────────────────────────────────

def _conexao_bd():
    import platform
    if platform.system() == "Windows":
        import pyodbc
        return pyodbc.connect(
            "DRIVER={SQL Server};"
            f"SERVER={_cfg('DB_SERVER')};"
            f"DATABASE={_cfg('DB_DATABASE')};"
            f"UID={_cfg('DB_USER')};"
            f"PWD={_cfg('DB_PASSWORD')};"
        )
    return pymssql.connect(
        server=_cfg("DB_SERVER"),
        port=int(_cfg("DB_PORT") or 1433),
        user=_cfg("DB_USER"),
        password=_cfg("DB_PASSWORD"),
        database=_cfg("DB_DATABASE"),
        login_timeout=5,
        timeout=10,
    )


def buscar_produto(codigo_interno: str):
    """
    Busca o produto pelo código interno.
    Retorna dict com 'num_fabricante' e 'descricao', ou None se não encontrado.
    """
    sql = """
        SELECT
            P.NumFabricante,
            UPPER(DP.Descricao),
            M.Imagem
        FROM Produtos P
        LEFT JOIN Marcas M ON M.Codigo = P.CodMarca
        LEFT JOIN DescricaoProduto DP ON DP.CodProduto = P.Codigo
        WHERE P.Codigo = ?
    """
    try:
        with _conexao_bd() as conn:
            cursor = conn.cursor()
            cursor.execute(sql, (int(codigo_interno),))
            row = cursor.fetchone()
            if row:
                return {
                    "num_fabricante": row[0] or "",
                    "descricao":      row[1] or "",
                    "imagem_marca":   row[2] or "",
                }
            return {}
    except Exception:
        return None


_BASE_URL_IMAGENS = "https://www.grupodinatec.com.br/imagens"


def buscar_imagens_produto(codigo: str) -> list[dict]:
    """
    Tenta baixar as variantes _1 a _5 da imagem do produto.
    Retorna lista de dicts com 'label' e 'bytes' para cada imagem encontrada.
    """
    encontradas = []
    for i in range(1, 6):
        url = f"{_BASE_URL_IMAGENS}/{codigo}_{i}.jpg"
        try:
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200 and resp.content:
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

def gerar_rodape(W, site, telefone, whatsapp, fator_s):
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

    # ── Listar contatos preenchidos ───────────────────────────────────────────
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

    # ── Calcular largura e altura do bloco de texto ───────────────────────────
    draw_tmp = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    linha_h  = draw_tmp.textbbox((0, 0), "Ag", font=fonte_valor)[3]
    texto_w  = 0
    if contatos:
        for label, valor in contatos:
            bb = draw_tmp.textbbox((0, 0), f"{label}: {valor}", font=fonte_label)
            texto_w = max(texto_w, bb[2] - bb[0])
    texto_h = len(contatos) * (linha_h + LINHA_SP) - LINHA_SP if contatos else 0

    # ── Calcular dimensões totais do rodapé ───────────────────────────────────
    bloco_w = logo_w + (GAP + texto_w if contatos else 0)
    bloco_h = max(logo_h, texto_h)
    TOTAL_H = bloco_h + 2 * PAD_V + cs(4)   # +4 = linha separadora

    # ── Criar canvas do rodapé ───────────────────────────────────────────────
    rodape = Image.new("RGB", (W, TOTAL_H), (41, 45, 150))
    draw   = ImageDraw.Draw(rodape)

    # Linha separadora no topo (cinza claro)
    draw.rectangle([0, 0, W, cs(3)], fill=(184, 184, 184))

    # Origem do bloco (centralizado horizontalmente e verticalmente)
    origem_x = (W - bloco_w) // 2
    origem_y = cs(4) + (TOTAL_H - cs(4) - bloco_h) // 2

    # ── Logo centralizada verticalmente no bloco ──────────────────────────────
    if logo_img:
        ly = origem_y + (bloco_h - logo_h) // 2
        rodape.paste(logo_img, (origem_x, ly), logo_img)

    # ── Texto de contatos centralizado verticalmente no bloco ─────────────────
    if contatos:
        txt_x   = origem_x + logo_w + GAP
        bloco_txt_h = len(contatos) * (linha_h + LINHA_SP) - LINHA_SP
        y_txt   = origem_y + (bloco_h - bloco_txt_h) // 2

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
               imagem_marca=""):
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

    # Retângulo marrom (lado esquerdo) — contém o código do produto
    CAIXA_CODIGO = [cx(7), cy(120), cx(247), cy(177)]
    draw.rectangle(CAIXA_CODIGO, fill=COR_AZUL)

    # Texto do código — italic bold branco, centralizado no retângulo marrom
    largura_codigo = CAIXA_CODIGO[2] - CAIXA_CODIGO[0] - cs(14)
    fonte_codigo   = fonte_auto_tamanho(draw, codigo, FONTS_BOLD_ITALIC,
                                        largura_codigo, tam_inicial=cs(32))
    centralizar_texto(draw, codigo, fonte_codigo, CAIXA_CODIGO, COR_BRANCO)

    # Área branca (lado direito) — contém o nome do produto
    CAIXA_NOME = [CAIXA_CODIGO[2] + 1, CAIXA_CODIGO[1], W - cx(7), CAIXA_CODIGO[3]]
    draw.rectangle(CAIXA_NOME, fill=COR_BRANCO)

    # Texto do nome — bold marrom, auto-ajusta tamanho, alinhado à esquerda
    largura_nome = CAIXA_NOME[2] - CAIXA_NOME[0] - cs(36)
    fonte_nome   = fonte_auto_tamanho(draw, nome, FONTS_BOLD,
                                      largura_nome, tam_inicial=cs(30))
    bb_nome = draw.textbbox((0, 0), nome, font=fonte_nome)
    # Centralizar verticalmente dentro da caixa, com margem à esquerda
    y_nome  = (CAIXA_NOME[1]
               + (CAIXA_NOME[3] - CAIXA_NOME[1] - (bb_nome[3] - bb_nome[1])) // 2
               - bb_nome[1])
    draw.text((CAIXA_NOME[0] + cs(18), y_nome), nome,
              font=fonte_nome, fill=COR_TEXTO)

    # ══════════════════════════════════════════════════════════════════════════
    # ZONA 3 — ÁREA DO PRODUTO (foto central)
    # Referência no template 913×915: x: 10–903, y: 178–690
    # Quando uma foto é enviada, a área inteira é limpa com branco e a foto
    # é centralizada dentro de uma zona com margem interna.
    # ══════════════════════════════════════════════════════════════════════════

    # Limites da área reservada para a foto no template
    AREA_X0 = cx(10);  AREA_Y0 = cy(178)
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
            # (apaga as fotos originais das peças e o espaço de veículos)
            draw.rectangle([0, AREA_Y0, W, RODAPE_Y_INICIO], fill=COR_BRANCO)

            # Colar a foto preservando transparência (alpha channel)
            base_rgba = canvas_base.convert("RGBA")
            base_rgba.paste(img_prod, (col_x, col_y), img_prod)
            canvas_base = base_rgba.convert("RGB")
            draw        = ImageDraw.Draw(canvas_base)

        except Exception:
            # Se falhar, mantém a área original do template (sem foto)
            foto_ok  = False
            fim_foto = AREA_Y1

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
    # Renderizado após a foto e limpeza da área, para não ser apagado.
    # ══════════════════════════════════════════════════════════════════════════
    if imagem_marca and imagem_marca.strip():
        _nome_arq_marca = imagem_marca.strip()
        _base_logos     = _cfg("LOGOS_PATH")
        _caminho_marca  = (_nome_arq_marca
                           if os.path.exists(_nome_arq_marca)
                           else os.path.join(_base_logos, _nome_arq_marca))
        try:
            if os.path.exists(_caminho_marca):
                _logo_marca = Image.open(_caminho_marca).convert("RGBA")
                MARCA_MAX_W = CAIXA_CODIGO[2] - CAIXA_CODIGO[0]
                MARCA_MAX_H = cs(80)
                _esc = min(MARCA_MAX_W / _logo_marca.width, MARCA_MAX_H / _logo_marca.height)
                _lw  = max(1, int(_logo_marca.width  * _esc))
                _lh  = max(1, int(_logo_marca.height * _esc))
                _logo_marca = _logo_marca.resize((_lw, _lh), Image.LANCZOS)
                _lx = CAIXA_CODIGO[0] + (MARCA_MAX_W - _lw) // 2
                _ly = CAIXA_CODIGO[3] + cs(20)
                _base_m = canvas_base.convert("RGBA")
                _base_m.paste(_logo_marca, (_lx, _ly), _logo_marca)
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
    rodape_custom = gerar_rodape(W, site, telefone, whatsapp, fator_s)
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

        return canvas_final

    else:
        # Sem foto: apagar o rodapé original do template e colar o personalizado
        NOVA_ALTURA  = RODAPE_Y_INICIO + RODAPE_CUSTOM_H
        canvas_final = Image.new("RGB", (W, NOVA_ALTURA), COR_BRANCO)
        recorte      = canvas_base.crop((0, 0, W, RODAPE_Y_INICIO))
        canvas_final.paste(recorte, (0, 0))
        canvas_final.paste(rodape_custom, (0, RODAPE_Y_INICIO))
        return canvas_final


# ══════════════════════════════════════════════════════════════════════════════
# INTERFACE STREAMLIT
# ══════════════════════════════════════════════════════════════════════════════

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
    background: #3b1f00; color: white; font-weight: 700;
    border: none; border-radius: 8px; padding: 11px 0;
    width: 100%; font-size: .9rem; margin-top: 6px;
    transition: background .2s;
}
div[data-testid="stDownloadButton"] > button:hover { background: #5a3200; }
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="titulo">🔧 Gerador de Promoção</div>', unsafe_allow_html=True)
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

    st.markdown("---")
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
    st.markdown("---")

    codigo   = campo_obrigatorio("🔢 Numero do Fabricante",     value=_cod_pre)
    nome     = campo_obrigatorio("📦 Descrição do produto",       value=_nome_pre)
    veiculos = campo_obrigatorio("🚛 Veículos compatíveis",  value="")

    st.markdown("---")
    st.markdown("### 🌐 Contatos da Empresa")
    site      = campo_obrigatorio("🌍 Site",      value="", placeholder="www.dinatec.com.br")
    telefone  = campo_obrigatorio("📞 Telefone",  value="", placeholder="(xx) xxxx-xxxx")
    whatsapp  = campo_obrigatorio("💬 WhatsApp",  value="", placeholder="(xx) xxxxx-xxxx")

    st.markdown("---")
    st.markdown("### 📦 Foto do Produto")

    foto_upload  = None
    _foto_bytes  = None

    if _imagens:
        opcoes = [img["label"] for img in _imagens]
        escolha = st.selectbox("🖼️ Imagens encontradas no site", opcoes)
        idx = opcoes.index(escolha)
        _foto_bytes = _imagens[idx]["bytes"]
        st.image(_imagens[idx]["url"], caption=escolha, width="content")
    elif codigo_interno.strip():
        st.warning(
            "⚠️ Nenhuma imagem encontrada no site para este código.\n\n"
            "Faça o carregamento manual abaixo."
        )

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

    st.markdown("""
    <div style="background:#fff8ee;border:1px solid #d4b07a;border-radius:8px;
                padding:12px 16px;margin-top:12px;font-size:.82rem;
                color:#5a3200;line-height:1.7;">
    💡 <b>Como usar:</b><br>
    1. Preencha os campos de texto acima<br>
    2. (Opcional) Envie a foto do produto<br>
    3. Veja a prévia ao lado<br>
    4. Baixe o card em <b>PNG</b> ou <b>JPG</b>
    </div>
    """, unsafe_allow_html=True)

# ── Gerar e exibir o card ─────────────────────────────────────────────────────
col_preview, col_download = st.columns([3, 1], gap="large")

faltando = [v for v in [badge, codigo, nome, veiculos, site, telefone, whatsapp]
            if not v.strip()]

if faltando:
    st.stop()

try:
    card = gerar_card(badge, codigo, nome, veiculos, foto_final, site, telefone, whatsapp,
                      imagem_marca=_imagem_marca)

    with col_preview:
        st.image(card, width='stretch',
                 caption="Prévia — atualiza automaticamente ao editar os campos")

    with col_download:
        st.markdown("### 💾 Exportar")

        # Botão PNG (sem perda de qualidade)
        buf_png = io.BytesIO()
        card.save(buf_png, format="PNG", dpi=(300, 300))
        buf_png.seek(0)
        st.markdown("#### FORMATO PNG")
        
        st.download_button(
            "Baixar (300 dpi)",
            data=buf_png,
            file_name=f"promo_{_nome_arquivo(codigo)}.png",
            mime="image/png",
        )

        # Botão JPG (menor tamanho de arquivo, qualidade 95%)
        buf_jpg = io.BytesIO()
        card.convert("RGB").save(buf_jpg, format="JPEG", quality=100, dpi=(300, 300))
        buf_jpg.seek(0)
        st.markdown("#### FORMATO JPG")        
        st.download_button(
            "Baixar (300 dpi)",
            data=buf_jpg,
            file_name=f"promo_{_nome_arquivo(codigo)}.jpg",
            mime="image/jpeg",
        )

except Exception as erro:
    with col_preview:
        st.error(f"Erro ao gerar o card:\n\n`{erro}`")
