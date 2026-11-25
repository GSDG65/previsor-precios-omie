# -*- coding: utf-8 -*-
"""
previsor_comun.py  ·  Librería común del previsor de precios OMIE
--------------------------------------------------------------------
Funciones compartidas por carga_historico.py y actualizacion_diaria.py:
configuración, descargas HTTP con reintentos, lectores de cada fuente
(OMIE, REData, MIBGAS, Open-Meteo) y almacenamiento en CSV.

Solo usa la librería estándar de Python.
"""
import csv, gzip, io, json, math, os, re, ssl, sys, time, zipfile
import datetime as dt
import urllib.request, urllib.error, urllib.parse

# ----------------------------------------------------------------------------
# Rutas
# ----------------------------------------------------------------------------
BASE = os.path.dirname(os.path.abspath(__file__))
DATOS = os.path.join(BASE, "datos")
METEO_PUNTOS = os.path.join(DATOS, "meteo_puntos")
LOGS = os.path.join(BASE, "logs")
CONFIG_PATH = os.path.join(BASE, "config_previsor.json")
for _d in (DATOS, METEO_PUNTOS, LOGS):
    os.makedirs(_d, exist_ok=True)

FECHA_INICIO = dt.date(2021, 1, 1)
HORAS = [f"h{h:02d}" for h in range(24)]

# ----------------------------------------------------------------------------
# Configuración por defecto (se escribe a config_previsor.json la primera vez;
# a partir de ahí se edita ese fichero, no este)
# Pesos = GW aproximados instalados en la zona que representa cada punto.
# Solo dan forma al índice: la escala absoluta se calibra con la generación
# real de REE, así que no necesitan ser exactos.
# ----------------------------------------------------------------------------
CONFIG_DEFECTO = {
    "puntos_eolicos": [
        ["Galicia N (Xistral)", 43.45, -7.55, 1.2], ["Galicia W (Coruña int.)", 43.10, -8.60, 1.2],
        ["Galicia S (Ourense/Lugo)", 42.35, -7.60, 1.4], ["Asturias", 43.35, -6.60, 0.6],
        ["Burgos (Páramos)", 42.70, -3.90, 2.6], ["Soria", 41.80, -2.50, 1.3],
        ["Zamora/León", 42.20, -6.20, 1.2], ["Palencia/Valladolid", 41.90, -4.60, 1.4],
        ["Zaragoza (Ebro/Moncayo)", 41.75, -1.30, 3.0], ["Teruel", 40.80, -0.90, 2.0],
        ["Huesca", 41.90, -0.20, 0.8], ["Navarra", 42.60, -1.70, 1.4], ["La Rioja", 42.30, -2.40, 0.5],
        ["Cataluña (Terra Alta)", 41.05, 0.45, 1.4], ["Albacete", 38.90, -1.80, 2.6],
        ["Cuenca", 39.80, -2.20, 1.4], ["Guadalajara", 40.90, -2.50, 0.8],
        ["C. Valenciana", 39.90, -0.40, 1.3], ["Cádiz (Tarifa)", 36.10, -5.65, 1.4],
        ["Málaga/Granada", 37.00, -3.20, 1.4], ["Almería", 37.10, -2.00, 0.6],
        ["Murcia", 38.40, -1.40, 0.3], ["Cantabria/País Vasco", 43.10, -3.00, 0.3]
    ],
    "puntos_solares": [
        ["Badajoz", 38.85, -6.60, 4.0], ["Cáceres", 39.50, -6.10, 2.5], ["Sevilla", 37.40, -5.60, 3.0],
        ["Córdoba/Jaén", 37.90, -4.30, 1.8], ["Almería/Granada", 37.20, -2.60, 1.2], ["Huelva", 37.40, -6.90, 1.0],
        ["Ciudad Real/Toledo", 39.20, -3.60, 3.0], ["Albacete/Cuenca FV", 39.30, -2.00, 2.0],
        ["Zaragoza FV", 41.50, -0.90, 3.0], ["Valladolid/Salamanca", 41.40, -4.90, 2.5],
        ["Murcia FV", 37.90, -1.20, 1.8], ["Alicante/Valencia FV", 38.60, -0.80, 1.0],
        ["Madrid FV", 40.20, -3.70, 0.6], ["Lleida", 41.60, 0.80, 0.8]
    ],
    "puntos_temperatura": [
        ["Madrid", 40.42, -3.70, 6.9], ["Barcelona", 41.39, 2.17, 5.7], ["Valencia", 39.47, -0.38, 2.6],
        ["Sevilla ciudad", 37.39, -5.98, 1.9], ["Málaga", 36.72, -4.42, 1.7], ["Alicante", 38.35, -0.48, 1.9],
        ["Murcia ciudad", 37.99, -1.13, 1.5], ["Bilbao", 43.26, -2.93, 1.15], ["Zaragoza", 41.65, -0.88, 1.0],
        ["A Coruña", 43.36, -8.41, 1.1], ["Valladolid", 41.65, -4.72, 0.5]
    ],
    "puntos_cuencas": [
        ["Miño-Sil", 42.60, -7.40, 3.0], ["Duero", 41.80, -5.00, 4.0], ["Tajo", 40.30, -5.00, 2.5],
        ["Ebro", 42.50, -1.00, 2.0], ["Pirineo", 42.60, 0.50, 1.5], ["Guadalquivir/Guadiana", 37.90, -4.00, 1.0]
    ],
    # Curva de potencia genérica (aerogenerador moderno, viento a 100 m)
    "curva_eolica": {"arranque": 3.0, "nominal": 12.0, "corte": 25.0},
    # CO2 (EUA, €/t) medio aproximado por año, para normalizar el coste del ciclo combinado.
    # Valores orientativos: corrígelos si tienes mejores datos. El año en curso se usa también como valor actual.
    "co2_anual_eur_t": {"2021": 53, "2022": 81, "2023": 84, "2024": 65, "2025": 73, "2026": 79.38},
    # CO2 (EUA, €/t) medio mensual (Sendeco2). Tiene prioridad sobre el anual. Para los meses posteriores al
    # último que figure se usa ese último valor. Añade cada mes su media para que el coste no se quede atrás.
    "co2_mensual_eur_t": {"2026-01": 86.08, "2026-02": 73.79, "2026-03": 68.73, "2026-04": 72.68,
                          "2026-05": 74.95, "2026-06": 77.93, "2026-07": 80.42, "2026-08": 81.59,
                          "2026-09": 85.04, "2026-10": 86.17, "2026-11": 86.38},
    # Coste marginal CCGT = gas / rendimiento + CO2 * factor_emision
    "ccgt_rendimiento": 0.53,
    "ccgt_factor_emision_t_mwh": 0.36,
    # Periodo excluido del aprendizaje de precio (crisis del gas + excepción ibérica, según lo acordado).
    # Los datos se descargan igualmente; solo no se usan para aprender precio.
    "excluir_desde": "2022-01-01",
    "excluir_hasta": "2022-12-31",
    # Open-Meteo: pausa entre puntos en la carga histórica (respeta el límite gratuito de 5.000 llamadas/hora)
    "openmeteo_pausa_s": 110
}


def co2_eur_t(cfg, fecha):
    """Precio del CO2 (€/t) para una fecha AAAA-MM-DD: media mensual si se conoce; si la fecha es posterior al
    último mes conocido, ese último mes; si no, la media anual (o la del último año disponible)."""
    men = cfg.get("co2_mensual_eur_t") or {}
    mes = fecha[:7]
    if mes in men:
        return float(men[mes])
    if men and mes > max(men):
        return float(men[max(men)])
    anual = cfg["co2_anual_eur_t"]
    return float(anual.get(fecha[:4], list(anual.values())[-1]))

def cargar_config():
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(CONFIG_DEFECTO, f, ensure_ascii=False, indent=2)
        return json.loads(json.dumps(CONFIG_DEFECTO))
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    for k, v in CONFIG_DEFECTO.items():      # claves nuevas que no estén en el fichero
        cfg.setdefault(k, v)
    return cfg

# ----------------------------------------------------------------------------
# Registro
# ----------------------------------------------------------------------------
_LOG_FILE = None

def iniciar_log(nombre):
    global _LOG_FILE
    _LOG_FILE = os.path.join(LOGS, f"{nombre}_{dt.datetime.now():%Y%m%d_%H%M}.log")

def log(msg):
    linea = f"{dt.datetime.now():%H:%M:%S}  {msg}"
    try:
        print(linea, flush=True)
    except UnicodeEncodeError:
        print(linea.encode("ascii", "replace").decode(), flush=True)
    if _LOG_FILE:
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(linea + "\n")

# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
UA = {"User-Agent": "Mozilla/5.0 (previsor-precios-omie)"}
_CTX = ssl.create_default_context()

class ErrorHTTP(Exception):
    def __init__(self, codigo, msg):
        super().__init__(f"HTTP {codigo}: {msg}")
        self.codigo = codigo

def descargar(url, intentos=4, timeout=60, espera_429=600):
    """Devuelve bytes. Reintenta errores de red y 5xx; en 429 espera y reintenta."""
    ultimo = None
    for i in range(intentos):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            cuerpo = ""
            try:
                cuerpo = e.read()[:300].decode("utf-8", "replace")
            except Exception:
                pass
            ultimo = ErrorHTTP(e.code, cuerpo)
            if e.code == 404:
                raise ultimo
            if e.code == 429:
                log(f"   límite de peticiones alcanzado; espero {espera_429 // 60} min…")
                time.sleep(espera_429)
                continue
            if e.code == 400 and "no están disponibles" not in cuerpo:
                raise ultimo
        except Exception as e:
            ultimo = e
        time.sleep(3 * (i + 1))
    raise ultimo

# ----------------------------------------------------------------------------
# Almacenamiento: CSV "ancho" por fecha (fecha;h00..h23) y CSV "largo" genérico
# ----------------------------------------------------------------------------
def leer_ancho(nombre):
    """{ 'YYYY-MM-DD': [24 floats|None] }"""
    ruta = os.path.join(DATOS, nombre)
    out = {}
    if not os.path.exists(ruta):
        return out
    with open(ruta, encoding="utf-8", newline="") as f:
        for fila in csv.DictReader(f, delimiter=";"):
            out[fila["fecha"]] = [float(fila[h]) if fila.get(h) not in (None, "") else None for h in HORAS]
    return out

def escribir_ancho(nombre, datos, decimales=2):
    ruta = os.path.join(DATOS, nombre)
    tmp = ruta + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["fecha"] + HORAS)
        for fecha in sorted(datos):
            w.writerow([fecha] + ["" if v is None else round(v, decimales) for v in datos[fecha]])
    os.replace(tmp, ruta)

def leer_tabla(nombre):
    """(cabecera, {clave_primera_columna: dict})"""
    ruta = os.path.join(DATOS, nombre)
    if not os.path.exists(ruta):
        return [], {}
    with open(ruta, encoding="utf-8", newline="") as f:
        r = csv.DictReader(f, delimiter=";")
        filas = {fila[r.fieldnames[0]]: fila for fila in r}
        return r.fieldnames, filas

def escribir_tabla(nombre, cabecera, filas):
    ruta = os.path.join(DATOS, nombre)
    tmp = ruta + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cabecera, delimiter=";", extrasaction="ignore")
        w.writeheader()
        for k in sorted(filas):
            w.writerow(filas[k])
    os.replace(tmp, ruta)

def a24(valores):
    """Normaliza un día de 23/25 horas (cambio de hora) a 24 posiciones 0..23."""
    v = list(valores)
    if len(v) == 25:      # octubre: la hora 02 aparece dos veces (posiciones 2 y 3)
        a, b = v[2], v[3]
        m = a if b is None else b if a is None else (a + b) / 2
        v = v[:2] + [m] + v[4:]
    elif len(v) == 23:    # marzo: falta la hora 02; se repite la 01
        v = v[:2] + [v[1]] + v[2:]
    return v[:24] + [None] * (24 - len(v))

def rango_fechas(desde, hasta):
    d = desde
    while d <= hasta:
        yield d
        d += dt.timedelta(days=1)

# ----------------------------------------------------------------------------
# OMIE · marginalpdbc  (año;mes;día;periodo;precio PT;precio ES;)
# ----------------------------------------------------------------------------
OMIE = "https://www.omie.es"

# Precios cuartohorarios (OMIE publica 96 periodos de 15 min desde oct-2025). parsear_marginalpdbc
# guarda aquí los cuartos de cada día que lee; se conservan en precios_omie_cuartohorario.csv.
CUARTOS = {}
CSV_CUARTOS = "precios_omie_cuartohorario.csv"
COLS_CUARTOS = [f"q{i:03d}" for i in range(1, 101)]

def leer_cuartos():
    """{ 'YYYY-MM-DD': [precios de 15 min] }"""
    ruta = os.path.join(DATOS, CSV_CUARTOS)
    out = {}
    if not os.path.exists(ruta):
        return out
    with open(ruta, encoding="utf-8", newline="") as f:
        for fila in csv.DictReader(f, delimiter=";"):
            n = int(fila["n"])
            out[fila["fecha"]] = [float(fila[c]) if fila.get(c) not in (None, "") else None for c in COLS_CUARTOS[:n]]
    return out

def escribir_cuartos(datos):
    ruta = os.path.join(DATOS, CSV_CUARTOS)
    tmp = ruta + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["fecha", "n"] + COLS_CUARTOS)
        for fecha in sorted(datos):
            v = datos[fecha]
            w.writerow([fecha, len(v)] + ["" if x is None else round(x, 2) for x in v] + [""] * (100 - len(v)))
    os.replace(tmp, ruta)

def parsear_marginalpdbc(texto):
    """{fecha: [24 precios ES horarios]}  · admite 23/24/25 periodos horarios y 92/96/100 cuartohorarios."""
    por_fecha = {}
    for ln in texto.splitlines():
        f = ln.strip().split(";")
        if len(f) < 6 or not f[0].isdigit() or len(f[0]) != 4:
            continue
        try:
            fecha = dt.date(int(f[0]), int(f[1]), int(f[2])).isoformat()
            p = int(f[3])
            precio = float(f[5].replace(",", "."))
        except ValueError:
            continue
        por_fecha.setdefault(fecha, {})[p] = precio
    out = {}
    for fecha, per in por_fecha.items():
        n = max(per)
        cuartos = n >= 90
        if cuartos:
            CUARTOS[fecha] = [per.get(i + 1) for i in range(n)]      # 96 (92/100 en cambio de hora)
        nh = n // 4 if cuartos else n
        horas = []
        for h in range(nh):
            if cuartos:
                vals = [per.get(h * 4 + q + 1) for q in range(4)]
                vals = [x for x in vals if x is not None]
                horas.append(sum(vals) / len(vals) if vals else None)
            else:
                horas.append(per.get(h + 1))
        out[fecha] = a24(horas)
    return out

def omie_listado_diario():
    """{ 'YYYYMMDD': version_max } a partir del listado público de ficheros."""
    url = (OMIE + "/es/file-access-list?parents=/Mercado%20Diario/1.%20Precios"
           "&dir=Precios%20horarios%20del%20mercado%20diario%20en%20Espa%C3%B1a&realdir=marginalpdbc")
    html = descargar(url).decode("utf-8", "replace")
    ver = {}
    for d, v in re.findall(r"marginalpdbc_(\d{8})\.(\d+)", html):
        ver[d] = max(ver.get(d, 0), int(v))
    return ver

def omie_dia(fecha, version=None):
    """Descarga un día (prueba la versión indicada y, si falla, las anteriores)."""
    d = fecha.strftime("%Y%m%d")
    versiones = range(version or 1, 0, -1)
    for v in versiones:
        try:
            b = descargar(f"{OMIE}/es/file-download?parents=marginalpdbc&filename=marginalpdbc_{d}.{v}", intentos=3)
            r = parsear_marginalpdbc(b.decode("latin-1", "replace"))
            if r:
                return r
        except ErrorHTTP as e:
            if e.codigo != 404:
                raise
    return {}

def omie_anual_zip(anio):
    """{fecha: [24]} de un zip anual (2021, 2022). Por fecha se toma la versión más alta."""
    b = descargar(f"{OMIE}/es/file-download?parents=marginalpdbc&filename=marginalpdbc_{anio}.zip", timeout=180)
    z = zipfile.ZipFile(io.BytesIO(b))
    mejor = {}
    for n in z.namelist():
        m = re.search(r"marginalpdbc_(\d{8})\.(\d+)", n)
        if m and int(m.group(2)) >= mejor.get(m.group(1), ("", 0))[1]:
            mejor[m.group(1)] = (n, int(m.group(2)))
    out = {}
    for _, (n, _) in mejor.items():
        out.update(parsear_marginalpdbc(z.read(n).decode("latin-1", "replace")))
    return out

# ----------------------------------------------------------------------------
# ENTSO-E Transparency Platform · Francia: precio day-ahead e intercambio físico con España
# La clave va en entsoe_token.txt (una sola línea). Nunca se escribe en los logs.
# ----------------------------------------------------------------------------
ENTSOE = "https://web-api.tp.entsoe.eu/api"
ZONA_FR = "10YFR-RTE------C"
ZONA_ES = "10YES-REE------0"
ZONA_PT = "10YPT-REN------W"
TOKEN_ENTSOE = os.path.join(BASE, "entsoe_token.txt")

def entsoe_token():
    if os.environ.get("ENTSOE_TOKEN", "").strip():          # GitHub Actions: secreto ENTSOE_TOKEN
        return os.environ["ENTSOE_TOKEN"].strip()
    if not os.path.exists(TOKEN_ENTSOE):
        return None
    with open(TOKEN_ENTSOE, encoding="utf-8-sig") as f:
        t = f.read().strip()
    return t or None

def entsoe_url(token, desde_utc, hasta_utc, **param):
    q = {"securityToken": token, "periodStart": desde_utc.strftime("%Y%m%d%H%M"),
         "periodEnd": hasta_utc.strftime("%Y%m%d%H%M"), **param}
    return ENTSOE + "?" + urllib.parse.urlencode(q)

def _ultimo_domingo(y, m):
    d = dt.datetime(y, m, 31)
    return d - dt.timedelta(days=(d.weekday() + 1) % 7)

def utc_a_local(t):
    """Hora de Madrid/París (CET/CEST, reglas de la UE) de un datetime UTC sin zona."""
    ini = _ultimo_domingo(t.year, 3).replace(hour=1)
    fin = _ultimo_domingo(t.year, 10).replace(hour=1)
    return t + dt.timedelta(hours=2 if ini <= t < fin else 1)

def parsear_entsoe(xml, campo, solo_generacion=False):
    """{hora UTC: media} de un documento de ENTSO-E (campo 'price.amount' o 'quantity').
    Admite resoluciones de 15/30/60 min y curvas A03 (posiciones omitidas = valor anterior)."""
    import xml.etree.ElementTree as ET
    raiz = ET.fromstring(xml)
    ns = raiz.tag.split("}")[0] + "}" if raiz.tag.startswith("{") else ""
    if raiz.tag.endswith("Acknowledgement_MarketDocument"):
        motivo = " ".join(t.text or "" for t in raiz.iter(f"{ns}text"))
        if "No matching data" in motivo:
            return {}
        raise ErrorHTTP(0, "ENTSO-E: " + motivo[:200])
    acum = {}
    periodos = []
    for ts in raiz.iter(f"{ns}TimeSeries"):
        # en A75 las series con outBiddingZone son consumo (bombeo, baterías): se descartan
        if solo_generacion and ts.find(f"{ns}outBiddingZone_Domain.mRID") is not None:
            continue
        periodos += ts.findall(f"{ns}Period")
    for per in periodos:
        ini = dt.datetime.strptime(per.findtext(f"{ns}timeInterval/{ns}start"), "%Y-%m-%dT%H:%MZ")
        fin = dt.datetime.strptime(per.findtext(f"{ns}timeInterval/{ns}end"), "%Y-%m-%dT%H:%MZ")
        paso = {"PT15M": 15, "PT30M": 30, "PT60M": 60}.get(per.findtext(f"{ns}resolution"))
        if not paso:
            continue
        pts = {}
        for p in per.findall(f"{ns}Point"):
            v = p.findtext(f"{ns}{campo}")
            if v is not None:
                pts[int(p.findtext(f"{ns}position"))] = float(v)
        ult = None
        for i in range(1, int((fin - ini).total_seconds() // 60 // paso) + 1):
            v = pts.get(i, ult)
            ult = v
            if v is None:
                continue
            t = ini + dt.timedelta(minutes=paso * (i - 1))
            acum.setdefault(t.replace(minute=0), []).append(v)
    return {h: sum(v) / len(v) for h, v in acum.items()}

def parsear_indisponibilidades(contenido):
    """Mensajes de indisponibilidad de ENTSO-E (A80/A77; zip o xml) →
    [{mrid, rev, cancelado, psr, unidad, nominal, tramos: [(ini_utc, fin_utc, MW disponibles)]}]"""
    import xml.etree.ElementTree as ET
    docs = []
    if contenido[:2] == b"PK":
        z = zipfile.ZipFile(io.BytesIO(contenido))
        docs = [z.read(n) for n in z.namelist() if n.lower().endswith(".xml")]
    else:
        docs = [contenido]
    loc = lambda e: e.tag.split("}")[-1]
    def hijo(e, nombre):
        return next((c for c in e.iter() if loc(c) == nombre), None)
    out = []
    for xml in docs:
        raiz = ET.fromstring(xml)
        if loc(raiz) == "Acknowledgement_MarketDocument":
            continue
        mrid = (hijo(raiz, "mRID").text or "").strip()
        rev = int((hijo(raiz, "revisionNumber").text or "1").strip())
        st = hijo(raiz, "docStatus")
        cancelado = st is not None and "A09" in "".join(st.itertext())
        for ts in [e for e in raiz.iter() if loc(e) == "TimeSeries"]:
            psr = next((c.text for c in ts.iter() if loc(c) == "production_RegisteredResource.pSRType.psrType"), None)
            nominal = next((float(c.text) for c in ts.iter() if loc(c).endswith("powerSystemResources.nominalP")), None)
            unidad = next((c.text for c in ts.iter() if loc(c).endswith("powerSystemResources.name")), None) \
                or next((c.text for c in ts.iter() if loc(c) == "production_RegisteredResource.name"), "")
            tramos = []
            for per in [e for e in ts.iter() if loc(e) == "Available_Period"]:
                ti = hijo(per, "timeInterval")
                ini = dt.datetime.strptime(hijo(ti, "start").text.strip(), "%Y-%m-%dT%H:%MZ")
                fin = dt.datetime.strptime(hijo(ti, "end").text.strip(), "%Y-%m-%dT%H:%MZ")
                res = (hijo(per, "resolution").text or "").strip()
                pts = sorted((int(hijo(p, "position").text), float(hijo(p, "quantity").text))
                             for p in per.iter() if loc(p) == "Point")
                if res in ("PT15M", "PT30M", "PT60M") and len(pts) > 1:
                    paso = dt.timedelta(minutes=int(res[2:-1]))
                    for pos, q in pts:
                        a = ini + paso * (pos - 1)
                        tramos.append((a, min(fin, a + paso), q))
                elif pts:                      # un único valor para todo el intervalo (lo habitual)
                    tramos.append((ini, fin, pts[0][1]))
            out.append({"mrid": mrid, "rev": rev, "cancelado": cancelado, "psr": psr, "unidad": unidad,
                        "nominal": nominal, "tramos": tramos})
    return out

def indisponible_horario(mensajes, psr="B14"):
    """{hora UTC: MW no disponibles} sumando unidades (última revisión de cada mensaje, sin cancelados)."""
    ult = {}
    for m in mensajes:
        if m["psr"] != psr or m["nominal"] is None:
            continue
        k = (m["mrid"], m["unidad"])
        if k not in ult or m["rev"] > ult[k]["rev"]:
            ult[k] = m
    por_unidad = {}
    for m in ult.values():
        if m["cancelado"]:
            continue
        u = por_unidad.setdefault(m["unidad"], {})
        for a, b, q in m["tramos"]:
            h = a.replace(minute=0)
            while h < b:
                falta = max(0.0, m["nominal"] - q)
                u[h] = max(u.get(h, 0.0), falta)     # mensajes solapados de la misma unidad: el mayor
                h += dt.timedelta(hours=1)
    total = {}
    for u in por_unidad.values():
        for h, v in u.items():
            total[h] = total.get(h, 0.0) + v
    return total

def utc_a_ancho(serie):
    """{hora UTC: valor} → {fecha local: [24]} solo con los días completos (23/24/25 horas)."""
    por_dia = {}
    for t in sorted(serie):
        por_dia.setdefault(utc_a_local(t).date().isoformat(), []).append(serie[t])
    return {f: a24(v) for f, v in por_dia.items() if len(v) in (23, 24, 25)}

# ----------------------------------------------------------------------------
# REE · REData  (apidatos.ree.es)
# ----------------------------------------------------------------------------
REE = "https://apidatos.ree.es/es/datos"
GEO = "&geo_trunc=electric_system&geo_limit=peninsular&geo_ids=8741"

def ree(widget, desde, hasta, trunc, geo=True):
    url = (f"{REE}/{widget}?start_date={desde.isoformat()}T00:00&end_date={hasta.isoformat()}T23:59"
           f"&time_trunc={trunc}" + (GEO if geo else ""))
    j = json.loads(descargar(url))
    if "errors" in j:
        raise ErrorHTTP(400, j["errors"][0].get("detail", ""))
    return {i["attributes"]["title"]: i["attributes"]["values"] for i in j.get("included", [])}

def parsear_horario_ree(valores):
    """Lista REE horaria → {fecha: [24]} usando la hora local que trae el propio datetime."""
    tmp = {}
    for v in valores:
        s = v["datetime"]                     # 2026-10-01T00:00:00.000+02:00
        fecha, h = s[:10], int(s[11:13])
        tmp.setdefault(fecha, {}).setdefault(h, []).append(float(v["value"]))
    out = {}
    for fecha, hs in tmp.items():
        fila = []
        for h in range(24):
            if h in hs:
                fila.append(sum(hs[h]) / len(hs[h]))
            else:
                fila.append(None)
        # hueco por cambio de hora en marzo: interpola
        for h in range(24):
            if fila[h] is None:
                a = next((fila[i] for i in range(h - 1, -1, -1) if fila[i] is not None), None)
                b = next((fila[i] for i in range(h + 1, 24) if fila[i] is not None), None)
                fila[h] = a if b is None else b if a is None else (a + b) / 2
        out[fecha] = fila
    return out

def parsear_diario_ree(series):
    """{titulo: valores} diario → {fecha: {titulo: valor}}"""
    out = {}
    for titulo, vals in series.items():
        for v in vals:
            out.setdefault(v["datetime"][:10], {})[titulo] = float(v["value"])
    return out

def parsear_mensual_ree(series):
    """{titulo: valores} mensual → {'YYYY-MM': {titulo: valor}}"""
    out = {}
    for titulo, vals in series.items():
        for v in vals:
            out.setdefault(v["datetime"][:7], {})[titulo] = float(v["value"])
    return out

# ----------------------------------------------------------------------------
# MIBGAS · CSV anual (Trading day;Product;…;First Day Delivery;Last Day Delivery;Price;)
# ----------------------------------------------------------------------------
MIBGAS = "https://www.mibgas.es"

def parsear_mibgas(texto):
    """{fecha_entrega: precio} del producto diario GDAES_D+1."""
    out = {}
    for ln in texto.splitlines():
        f = ln.split(";")
        if len(f) < 7 or f[1].strip() != "GDAES_D+1":
            continue
        try:
            d = dt.datetime.strptime(f[4].strip(), "%d/%m/%Y").date().isoformat()
            out[d] = float(f[6].strip().replace(",", "."))
        except ValueError:
            continue
    return out

def mibgas_anio(anio):
    b = descargar(f"{MIBGAS}/es/file-access/MIBGAS_Data_{anio}.csv?path=AGNO_{anio}/XLS")
    return parsear_mibgas(b.decode("latin-1", "replace"))

def rellenar_hacia_delante(serie, desde, hasta):
    """Completa fines de semana/festivos con el último valor conocido."""
    out = {}
    ultimo = serie[min(serie)] if serie else None      # antes del primer dato: primer valor conocido
    for d in rango_fechas(desde, hasta):
        k = d.isoformat()
        if k in serie:
            ultimo = serie[k]
        if ultimo is not None:
            out[k] = ultimo
    return out

# ----------------------------------------------------------------------------
# Open-Meteo
# ----------------------------------------------------------------------------
OM_ARCHIVO = "https://archive-api.open-meteo.com/v1/archive"
OM_PREVISION = "https://api.open-meteo.com/v1/forecast"
VARS_HORARIAS = "wind_speed_100m,shortwave_radiation,temperature_2m,precipitation"

def om_url(base, lat, lon, extra):
    return (f"{base}?latitude={lat}&longitude={lon}&hourly={VARS_HORARIAS}"
            f"&wind_speed_unit=ms&timezone=Europe%2FMadrid&{extra}")

def parsear_om(j):
    """JSON Open-Meteo → {fecha: {var: [24]}} (hora local; 23/25 h normalizadas)."""
    hr = j["hourly"]
    tmp = {}
    for i, t in enumerate(hr["time"]):            # 2021-01-01T00:00 (hora local)
        fecha, h = t[:10], int(t[11:13])
        dia = tmp.setdefault(fecha, {})
        for var in VARS_HORARIAS.split(","):
            v = hr.get(var, [None] * len(hr["time"]))[i]
            dia.setdefault(var, {}).setdefault(h, []).append(v)
    out = {}
    for fecha, vs in tmp.items():
        out[fecha] = {}
        for var, hs in vs.items():
            fila = []
            for h in range(24):
                xs = [x for x in hs.get(h, []) if x is not None]
                fila.append(sum(xs) / len(xs) if xs else None)
            # hueco de la hora 02 en el cambio de hora de marzo: interpola
            for h in range(1, 23):
                if fila[h] is None and h not in hs and fila[h - 1] is not None and fila[h + 1] is not None:
                    fila[h] = (fila[h - 1] + fila[h + 1]) / 2
            out[fecha][var] = fila
    return out

# Varios modelos a la vez (parámetro models=): la respuesta trae cada variable con el sufijo _<modelo>.
# Evaluación (evaluar_meteo.py, nov-2026): la media de estos modelos acierta mejor el precio a D+4/D+5.
MODELOS_METEO = ["ecmwf_ifs025", "ecmwf_aifs025_single", "icon_seamless", "meteofrance_seamless",
                 "gfs_seamless", "ukmo_seamless"]

def parsear_om_modelos(j, modelos):
    """JSON Open-Meteo con varios modelos → {modelo: {fecha: {var: [24]}}}"""
    hr = j["hourly"]
    out = {}
    for m in modelos:
        sub = {"time": hr["time"]}
        for var in VARS_HORARIAS.split(","):
            if var + "_" + m in hr:
                sub[var] = hr[var + "_" + m]
        if len(sub) > 1:
            out[m] = parsear_om({"hourly": sub})
    return out

def ruta_punto(nombre):
    limpio = re.sub(r"[^A-Za-z0-9]+", "_", nombre).strip("_").lower()
    return os.path.join(METEO_PUNTOS, f"{limpio}.csv.gz")

def leer_punto(nombre):
    """{fecha: {var: [24]}}"""
    ruta = ruta_punto(nombre)
    out = {}
    if not os.path.exists(ruta):
        return out
    with gzip.open(ruta, "rt", encoding="utf-8", newline="") as f:
        for fila in csv.reader(f, delimiter=";"):
            if fila[0] == "fecha":
                continue
            fecha, var = fila[0], fila[1]
            out.setdefault(fecha, {})[var] = [float(x) if x != "" else None for x in fila[2:26]]
    return out

def escribir_punto(nombre, datos):
    ruta = ruta_punto(nombre)
    tmp = ruta + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["fecha", "variable"] + HORAS)
        for fecha in sorted(datos):
            for var in sorted(datos[fecha]):
                w.writerow([fecha, var] + ["" if x is None else round(x, 2) for x in datos[fecha][var]])
    os.replace(tmp, ruta)

def todos_los_puntos(cfg):
    """Lista única de (nombre, lat, lon) de todos los grupos."""
    vistos, out = {}, []
    for grupo in ("puntos_eolicos", "puntos_solares", "puntos_temperatura", "puntos_cuencas",
                  "puntos_eolicos_pt", "puntos_solares_pt", "puntos_temperatura_pt"):
        for nombre, lat, lon, _ in cfg.get(grupo, []):
            if nombre in vistos:
                if vistos[nombre] != (lat, lon):
                    log(f"   ! el nombre de punto '{nombre}' se repite con otras coordenadas; renómbralo en config_previsor.json")
                continue
            vistos[nombre] = (lat, lon)
            out.append((nombre, lat, lon))
    return out

# ----------------------------------------------------------------------------
# Índices peninsulares a partir de los puntos
# ----------------------------------------------------------------------------
def cf_eolico(v, curva):
    """Factor de capacidad 0..1 con curva genérica (cúbica entre arranque y nominal)."""
    if v is None:
        return None
    a, n, c = curva["arranque"], curva["nominal"], curva["corte"]
    if v < a or v >= c:
        return 0.0
    if v >= n:
        return 1.0
    return (v ** 3 - a ** 3) / (n ** 3 - a ** 3)

def cf_solar(ghi, temp):
    """Índice FV 0..~1: radiación global / 1000 con pérdida térmica sencilla."""
    if ghi is None:
        return None
    t = 15.0 if temp is None else temp
    tcel = t + ghi / 800 * 25          # aprox. temperatura de célula
    return max(0.0, ghi / 1000 * (1 - 0.004 * (tcel - 25)))

def indices_dia(cfg, puntos, fecha):
    """Para una fecha: dict con listas de 24 → eol, sol, temp (pond. población) y precipitación diaria de cuencas."""
    def media_pond(grupo, fn):
        res = []
        for h in range(24):
            num = den = 0.0
            for nombre, _, _, peso in cfg.get(grupo, []):
                d = puntos.get(nombre, {}).get(fecha)
                if not d:
                    continue
                x = fn(d, h)
                if x is not None:
                    num += peso * x
                    den += peso
            res.append(num / den if den else None)
        return res

    curva = cfg["curva_eolica"]
    eol = media_pond("puntos_eolicos", lambda d, h: cf_eolico(d["wind_speed_100m"][h], curva))
    sol = media_pond("puntos_solares", lambda d, h: cf_solar(d["shortwave_radiation"][h], d["temperature_2m"][h]))
    tmp = media_pond("puntos_temperatura", lambda d, h: d["temperature_2m"][h])
    num = den = 0.0
    for nombre, _, _, peso in cfg["puntos_cuencas"]:
        d = puntos.get(nombre, {}).get(fecha)
        if d and any(x is not None for x in d["precipitation"]):
            num += peso * sum(x for x in d["precipitation"] if x is not None)
            den += peso
    out = {"eol": eol, "sol": sol, "temp": tmp, "lluvia": (num / den) if den else None}
    # Portugal (mismo mercado MIBEL): índices propios, aún no usados por el modelo de precios
    if cfg.get("puntos_eolicos_pt"):
        out["eol_pt"] = media_pond("puntos_eolicos_pt", lambda d, h: cf_eolico(d["wind_speed_100m"][h], curva))
        out["sol_pt"] = media_pond("puntos_solares_pt", lambda d, h: cf_solar(d["shortwave_radiation"][h], d["temperature_2m"][h]))
        out["temp_pt"] = media_pond("puntos_temperatura_pt", lambda d, h: d["temperature_2m"][h])
    return out

# ----------------------------------------------------------------------------
# Calendario: festivos nacionales (fijos + Viernes Santo)
# ----------------------------------------------------------------------------
def _pascua(y):
    a = y % 19; b = y // 100; c = y % 100; d = b // 4; e = b % 4
    f = (b + 8) // 25; g = (b - f + 1) // 3; h = (19 * a + b - d - g + 15) % 30
    i = c // 4; k = c % 4; l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mes = (h + l - 7 * m + 114) // 31; dia = ((h + l - 7 * m + 114) % 31) + 1
    return dt.date(y, mes, dia)

def festivos_nacionales(y):
    fijos = [(1, 1), (1, 6), (5, 1), (8, 15), (10, 12), (11, 1), (12, 6), (12, 8), (12, 25)]
    s = {dt.date(y, m, d) for m, d in fijos}
    s.add(_pascua(y) - dt.timedelta(days=2))     # Viernes Santo
    return s

def tipo_dia(fecha):
    """0 laborable · 1 sábado · 2 domingo/festivo · 3 puente (laborable entre festivo y fin de semana)"""
    fest = festivos_nacionales(fecha.year)
    if fecha in fest or fecha.weekday() == 6:
        return 2
    if fecha.weekday() == 5:
        return 1
    ayer, man = fecha - dt.timedelta(days=1), fecha + dt.timedelta(days=1)
    if (ayer in fest and man.weekday() >= 5) or (man in fest and ayer.weekday() >= 5):
        return 3
    return 0
