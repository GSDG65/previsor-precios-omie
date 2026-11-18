# -*- coding: utf-8 -*-
"""
previsor_modelo.py  ·  Modelo de previsión de precios OMIE (días análogos)
---------------------------------------------------------------------------
Idea:
  1. Para cada hora se calcula la DEMANDA RESIDUAL TÉRMICA
        R = demanda − eólica − solar FV − nuclear       (MW)
     La eólica y la FV salen de los índices meteo × potencia efectiva
     (calibrada contra la generación real de REE de los 60 días previos).
  2. El precio se expresa como RATIO sobre el coste del ciclo combinado
        ratio = precio / (gas/η + CO2·fe)
     Así son comparables días con gas a 25 y a 90 €/MWh.
  3. Para un día a prever se buscan los días pasados más parecidos en
     R por franja + llenado de embalses + estación + tipo de día,
     ponderando más lo reciente. Sus ratios dan la horquilla P10–P50–P90,
     que se multiplica por el coste actual del ciclo.
  4. Correcciones que aprende solo:
       · nivel reciente: cómo ha errado el modelo los últimos 14 días reales
       · calibración por horizonte y franja a partir de las previsiones congeladas
  5. Explicación: una regresión lineal sencilla reparte la diferencia frente
     a las últimas 4 semanas entre viento, sol, demanda, nuclear, embalses,
     tipo de día y gas.
"""
import math, json, os
import datetime as dt
from previsor_comun import *

FRANJAS = [("madrugada", 0, 7), ("subida", 7, 10), ("valle_solar", 10, 15),
           ("sostenido", 15, 19), ("pico", 19, 23), ("cierre", 23, 24)]
NOMBRES_FRANJA = {"madrugada": "Madrugada 0–7", "subida": "Subida 7–10", "valle_solar": "Valle solar 10–15",
                  "sostenido": "Sostenido 15–19", "pico": "Pico 19–23", "cierre": "Cierre 23–24"}

def _f(x):
    try:
        return float(x) if x not in (None, "") else None
    except ValueError:
        return None

def media(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None

def cuantil_pond(valores, pesos, q):
    par = sorted((v, w) for v, w in zip(valores, pesos) if v is not None and w > 0)
    if not par:
        return None
    tot = sum(w for _, w in par)
    acum = 0.0
    for v, w in par:
        acum += w
        if acum >= q * tot:
            return v
    return par[-1][0]

# ============================================================================
# Base de datos en memoria
# ============================================================================
class Base:
    def __init__(self, cfg):
        self.cfg = cfg
        self.precio = leer_ancho("precios_omie_horario.csv")
        self.dem = leer_ancho("demanda_horaria.csv")
        self.eolI = leer_ancho("meteo_indice_eolico.csv")
        self.solI = leer_ancho("meteo_indice_solar.csv")
        self.temp = leer_ancho("meteo_temperatura.csv")
        _, gen = leer_tabla("generacion_diaria.csv")
        _, gas = leer_tabla("gas_mibgas.csv")
        _, hid = leer_tabla("hidraulica_mensual.csv")
        self.gen = {f: {k: _f(v) for k, v in r.items() if k != "fecha"} for f, r in gen.items()}
        self.gas = {f: _f(r["gas_eur_mwh"]) for f, r in gas.items()}
        self.hid = {m: _f(r.get("llenado_total_pct")) for m, r in hid.items()}
        self.meses_hid = sorted(m for m, v in self.hid.items() if v is not None)
        self.fechas = sorted(set(self.precio) | set(self.dem) | set(self.eolI))
        self._cap = {}
        self._calibrar_capacidades()

    # --- datos auxiliares -----------------------------------------------------
    def llenado(self, fecha):
        prev = [m for m in self.meses_hid if m <= fecha[:7]]
        return self.hid[prev[-1]] if prev else None

    def coste_ccgt(self, fecha, gas=None):
        g = gas if gas is not None else self.gas.get(fecha)
        if g is None:
            return None
        co2 = self.cfg["co2_anual_eur_t"]
        c = co2.get(fecha[:4], list(co2.values())[-1])
        return g / self.cfg["ccgt_rendimiento"] + c * self.cfg["ccgt_factor_emision_t_mwh"]

    def nuclear_mw(self, fecha):
        v = self.gen.get(fecha, {}).get("Nuclear")
        return v / 24 if v is not None else None

    def ultimo_con(self, serie, hasta):
        ks = [k for k in serie if k <= hasta and serie[k] is not None]
        return max(ks) if ks else None

    # --- potencia efectiva (MW por unidad de índice), ventana móvil de 60 días previos
    def _calibrar_capacidades(self):
        dias = sorted(f for f in self.gen if f in self.eolI and f in self.solI)
        acc = []
        for f in dias:
            g = self.gen[f]
            e, s = g.get("Eólica"), g.get("Solar fotovoltaica")
            ie, is_ = media(self.eolI[f]), media(self.solI[f])
            if None in (e, s, ie, is_):
                continue
            acc.append((f, e / 24, ie, s / 24, is_))
        for i, (f, *_r) in enumerate(acc):
            ventana = acc[max(0, i - 60):i] or acc[:1]
            se = sum(x[1] for x in ventana); sie = sum(x[2] for x in ventana)
            ss = sum(x[3] for x in ventana); sis = sum(x[4] for x in ventana)
            self._cap[f] = (se / sie if sie else None, ss / sis if sis else None)
        self._cap_fechas = sorted(self._cap)
        # capacidad "actual": ventana de los 60 últimos días con dato
        ult = acc[-60:]
        if ult:
            self.cap_actual = (sum(x[1] for x in ult) / sum(x[2] for x in ult),
                               sum(x[3] for x in ult) / sum(x[4] for x in ult))
        else:
            self.cap_actual = (30000.0, 25000.0)

    def capacidad(self, fecha):
        if fecha in self._cap:
            return self._cap[fecha]
        prev = [k for k in self._cap_fechas[-90:] if k <= fecha]
        return self._cap[prev[-1]] if prev else self.cap_actual

    # --- vector horario de demanda residual (MW) -----------------------------
    def residual(self, dem, eolI, solI, nuc, cap):
        ce, cs = cap
        return [dem[h] - eolI[h] * ce - solI[h] * cs - (nuc or 0) for h in range(24)]

    def dia_historico(self, fecha):
        """Entradas reales de un día pasado (o None si faltan datos)."""
        if not all(fecha in s for s in (self.dem, self.eolI, self.solI)):
            return None
        dem, ei, si = self.dem[fecha], self.eolI[fecha], self.solI[fecha]
        if any(x is None for x in dem + ei + si):
            return None
        nuc = self.nuclear_mw(fecha)
        if nuc is None:
            return None
        cap = self.capacidad(fecha)
        return {"fecha": fecha, "dem": dem, "eolI": ei, "solI": si, "nuc": nuc, "cap": cap,
                "temp": self.temp.get(fecha), "llenado": self.llenado(fecha),
                "R": self.residual(dem, ei, si, nuc, cap), "tipo": tipo_dia(dt.date.fromisoformat(fecha))}

# ============================================================================
# Rasgos y búsqueda de análogos
# ============================================================================
def rasgos(d):
    """Vector de rasgos de un día: R medio por franja (GW), llenado, estación, fin de semana."""
    R = d["R"]
    v = [media(R[a:b]) / 1000 for _, a, b in FRANJAS]
    doy = dt.date.fromisoformat(d["fecha"]).timetuple().tm_yday
    v += [(d["llenado"] or 50) / 10,
          math.cos(2 * math.pi * doy / 365.25), math.sin(2 * math.pi * doy / 365.25),
          1.0 if d["tipo"] in (1, 2) else 0.0]
    return v

PESOS_RASGOS = [1.0, 1.0, 1.3, 1.0, 1.2, 0.6,      # franjas (más peso al valle solar y al pico)
                0.8,                               # embalses
                0.5, 0.5,                          # estación
                0.6]                               # fin de semana / festivo

class Analogos:
    """Catálogo de días históricos con rasgos y ratios, para buscar análogos."""
    def __init__(self, base):
        self.base = base
        cfg = base.cfg
        self.dias = []
        for f in base.fechas:
            if cfg["excluir_desde"] <= f <= cfg["excluir_hasta"]:
                continue
            p = base.precio.get(f)
            if not p or any(x is None for x in p):
                continue
            c = base.coste_ccgt(f)
            d = base.dia_historico(f)
            if d is None or not c:
                continue
            d["ratio_h"] = [x / c for x in p]
            d["ratio_f"] = {n: media(p[a:b]) / c for n, a, b in FRANJAS}
            d["ratio_dia"] = media(p) / c
            d["precio_medio"] = media(p)
            d["rasgos"] = rasgos(d)
            self.dias.append(d)
        # escala de cada rasgo (desviación típica)
        n = len(self.dias[0]["rasgos"]) if self.dias else 0
        self.escala = []
        for i in range(n):
            xs = [d["rasgos"][i] for d in self.dias]
            m = sum(xs) / len(xs)
            sd = math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs)) or 1.0
            self.escala.append(sd)

    def buscar(self, d, antes_de, k=40, vida_media_dias=365):
        """Análogos de d entre días < antes_de. Devuelve [(dia, peso)]."""
        x = rasgos(d)
        ref = dt.date.fromisoformat(antes_de)
        cand = []
        for h in self.dias:
            if h["fecha"] >= antes_de:
                continue
            dist = math.sqrt(sum(PESOS_RASGOS[i] * ((x[i] - h["rasgos"][i]) / self.escala[i]) ** 2
                                 for i in range(len(x))))
            edad = (ref - dt.date.fromisoformat(h["fecha"])).days
            rec = 0.5 ** (edad / vida_media_dias)
            cand.append((dist - 0.35 * math.log(rec), dist, rec, h))   # la antigüedad penaliza un poco la cercanía
        cand.sort(key=lambda t: t[0])
        sel = cand[:k]
        if not sel:
            return []
        dmax = max(t[1] for t in sel) * 1.05 or 1.0
        out = []
        for _, dist, rec, h in sel:
            kern = (1 - (dist / dmax) ** 3) ** 3          # tricúbico
            out.append((h, max(kern, 0.02) * rec))
        return out

# Corrección local por demanda residual: los análogos rara vez son tan extremos como el día a prever
# (mucho o poco viento), así que su mediana queda "comprimida" hacia lo normal. Se ajusta una recta
# ratio ~ R entre los análogos (ponderada) y se desplaza la horquilla FACTOR × pendiente × (R_obj − R̄_análogos).
# Factor 0 = desactivado; se puede cambiar con "correccion_residual" en config_previsor.json.
CORRECCION_RESIDUAL = 0.5
CORRECCION_RESIDUAL_MAX = 0.3       # tope del desplazamiento, en ratio sobre el coste del ciclo
# Riesgo de precio cero en el valle solar: si al menos esta fracción (ponderada) de los análogos tuvo
# el valle por debajo de UMBRAL_CERO_EUR, no se aplica sesgo positivo al valle y su P10 baja hasta 0.
UMBRAL_CERO_EUR = 5.0
FRACCION_CERO = 0.25

def _recta_pond(xs, ys, ws):
    sw = sum(ws)
    mx = sum(x * w for x, w in zip(xs, ws)) / sw
    my = sum(y * w for y, w in zip(ys, ws)) / sw
    vx = sum(w * (x - mx) ** 2 for x, w in zip(xs, ws)) / sw
    if vx < 1e-9:
        return 0.0, mx
    return sum(w * (x - mx) * (y - my) for x, y, w in zip(xs, ys, ws)) / sw / vx, mx

def prever_dia(analogos, d, antes_de, coste, ajuste=None, ensanche=1.0):
    """Horquillas por franja, por hora y del día completo para un día con entradas d.
       ajuste: {franja: sesgo_en_ratio} (se suma)
       ensanche: factor sobre la anchura P10–P90; número único o {franja: f, "dia": f}."""
    an = analogos.buscar(d, antes_de)
    pesos = [w for _, w in an]
    ajuste = ajuste or {}
    def ens_de(n):
        if isinstance(ensanche, dict):
            return ensanche.get(n, ensanche.get("_global", 1.0))
        return ensanche
    res = {"franjas": {}, "horas": {"p10": [], "p50": [], "p90": []},
           "analogos": sorted(([h["fecha"], round(w, 4), round(h["precio_medio"], 2)] for h, w in an),
                             key=lambda t: -t[1])[:10]}
    # --- corrección local por demanda residual (por franja y para el día completo)
    fac = analogos.base.cfg.get("correccion_residual", CORRECCION_RESIDUAL) if hasattr(analogos, "base") else CORRECCION_RESIDUAL
    delta = {n: 0.0 for n, _, _ in FRANJAS}
    delta["dia"] = 0.0
    if fac and an and sum(pesos) > 0:
        tramos = [(n, a, b) for n, a, b in FRANJAS] + [("dia", 0, 24)]
        for n, a, b in tramos:
            xs = [media(h["R"][a:b]) / 1000 for h, _ in an]
            ys = [h["ratio_dia"] if n == "dia" else h["ratio_f"][n] for h, _ in an]
            pend, mx = _recta_pond(xs, ys, pesos)
            dl = fac * pend * (media(d["R"][a:b]) / 1000 - mx)
            delta[n] = max(-CORRECCION_RESIDUAL_MAX, min(CORRECCION_RESIDUAL_MAX, dl))
    # --- riesgo de precio cero en el valle solar
    sw = sum(pesos)
    frac_cero = (sum(w for h, w in an if h["ratio_f"]["valle_solar"] * h["precio_medio"] / h["ratio_dia"] < UMBRAL_CERO_EUR) / sw
                 if sw and an else 0.0)
    cero = frac_cero >= FRACCION_CERO
    res["fraccion_cero_valle"] = round(frac_cero, 3)
    def horquilla(vals, sesgo, ens, dl=0.0, valle=False):
        p10, p50, p90 = (cuantil_pond(vals, pesos, q) for q in (0.1, 0.5, 0.9))
        if valle and cero:
            sesgo = min(sesgo, 0.0)          # un sesgo al alza aleja la previsión del cero
        p50 += sesgo + dl
        p10 = p50 - (p50 - (p10 + sesgo + dl)) * ens
        p90 = p50 + ((p90 + sesgo + dl) - p50) * ens
        if valle and cero:
            p10 = min(p10, 0.0)
        return [p10 * coste, p50 * coste, p90 * coste]
    for n, a, b in FRANJAS:
        res["franjas"][n] = [round(x, 2) for x in horquilla([h["ratio_f"][n] for h, _ in an], ajuste.get(n, 0.0),
                                                           ens_de(n), delta[n], n == "valle_solar")]
    for hh in range(24):
        n = next(n for n, a, b in FRANJAS if a <= hh < b)
        p10, p50, p90 = horquilla([h["ratio_h"][hh] for h, _ in an], ajuste.get(n, 0.0), ens_de(n),
                                  delta[n], n == "valle_solar")
        res["horas"]["p10"].append(round(p10, 2)); res["horas"]["p50"].append(round(p50, 2)); res["horas"]["p90"].append(round(p90, 2))
    # Día completo: cuantiles del precio medio diario de los análogos (no la media de las horquillas
    # horarias, que exagera la anchura porque los extremos de cada hora no ocurren todos a la vez)
    sesgo_dia = sum(ajuste.get(n, 0.0) * (b - a) for n, a, b in FRANJAS) / 24
    res["dia"] = [round(x, 2) for x in horquilla([h["ratio_dia"] for h, _ in an], sesgo_dia, ens_de("dia"), delta["dia"])]
    return res

# ============================================================================
# Corrección de nivel reciente: error del modelo (con entradas reales) en los últimos días
# ============================================================================
def sesgo_reciente(base, analogos, hasta, dias=14):
    errores = {n: [] for n, _, _ in FRANJAS}
    pesos = []
    fechas = [h["fecha"] for h in analogos.dias if h["fecha"] < hasta][-dias:]
    for i, f in enumerate(fechas):
        h = next(x for x in analogos.dias if x["fecha"] == f)
        r = prever_dia(analogos, h, f, 1.0)            # coste 1 → resultados en ratio
        w = 0.85 ** (len(fechas) - 1 - i)
        pesos.append(w)
        for n, _, _ in FRANJAS:
            errores[n].append(h["ratio_f"][n] - r["franjas"][n][1])
    if not pesos:
        return {n: 0.0 for n, _, _ in FRANJAS}
    return {n: sum(e * w for e, w in zip(errores[n], pesos)) / sum(pesos) for n, _, _ in FRANJAS}

# ============================================================================
# Previsión de demanda horaria
# ============================================================================
def prever_demanda(base, fecha, temp_media, antes_de):
    """Media ponderada de días parecidos (tipo de día, temperatura, época) de los últimos 2 años."""
    obj = dt.date.fromisoformat(fecha)
    tipo = tipo_dia(obj)
    ref = dt.date.fromisoformat(antes_de)
    cand = []
    for f in base.dem:
        if f >= antes_de:
            continue
        d = dt.date.fromisoformat(f)
        edad = (ref - d).days
        if edad > 730 or f not in base.temp:
            continue
        t = tipo_dia(d)
        if (t in (1, 2)) != (tipo in (1, 2)):
            continue
        if tipo in (1, 2) and t != tipo:
            pen = 1.0
        elif t != tipo:
            pen = 0.6            # puente frente a laborable
        else:
            pen = 0.0
        tm = media(base.temp[f])
        ddoy = abs(obj.timetuple().tm_yday - d.timetuple().tm_yday)
        ddoy = min(ddoy, 365 - ddoy)
        dist = abs(tm - temp_media) / 2.5 + ddoy / 25 + pen
        cand.append((dist, edad, f))
    cand.sort()
    sel = cand[:12]
    if not sel:
        return None
    pesos = [0.5 ** (e / 365) / (1 + d) for d, e, _ in sel]
    perfil = [sum(base.dem[f][h] * w for (_, _, f), w in zip(sel, pesos)) / sum(pesos) for h in range(24)]
    return perfil

def factor_nivel_demanda(base, antes_de, dias=14):
    """Corrige el nivel con el error de las 2 últimas semanas (electrificación, actividad…)."""
    reales, previstos = [], []
    fechas = sorted(f for f in base.dem if f < antes_de and f in base.temp)[-dias:]
    for f in fechas:
        p = prever_demanda(base, f, media(base.temp[f]), f)
        if p:
            reales.append(media(base.dem[f])); previstos.append(media(p))
    if not reales:
        return 1.0
    return max(0.9, min(1.1, sum(reales) / sum(previstos)))

# ============================================================================
# Explicación por factores (regresión lineal ponderada sobre el ratio diario)
# ============================================================================
VARS_EXPL = [("eolica", "Viento"), ("solar", "Sol"), ("demanda", "Demanda"),
             ("nuclear", "Nuclear"), ("llenado", "Embalses"), ("finde", "Tipo de día")]

def _vars_expl(d):
    ce, cs = d["cap"]
    return {"eolica": media(d["eolI"]) * ce / 1000, "solar": media(d["solI"]) * cs / 1000,
            "demanda": media(d["dem"]) / 1000, "nuclear": d["nuc"] / 1000,
            "llenado": (d["llenado"] or 50) / 10, "finde": 1.0 if d["tipo"] in (1, 2) else 0.0}

def _resolver(A, b):
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[p] = M[p], M[c]
        if abs(M[c][c]) < 1e-12:
            continue
        for r in range(n):
            if r != c:
                f = M[r][c] / M[c][c]
                for k in range(c, n + 1):
                    M[r][k] -= f * M[c][k]
    return [M[i][n] / M[i][i] if abs(M[i][i]) > 1e-12 else 0.0 for i in range(n)]

def ajustar_explicacion(analogos, antes_de):
    ref = dt.date.fromisoformat(antes_de)
    X, y, w = [], [], []
    for h in analogos.dias:
        if h["fecha"] >= antes_de:
            continue
        edad = (ref - dt.date.fromisoformat(h["fecha"])).days
        if edad > 3 * 365:
            continue
        v = _vars_expl(h)
        X.append([1.0] + [v[k] for k, _ in VARS_EXPL]); y.append(h["ratio_dia"]); w.append(0.5 ** (edad / 365))
    n = len(X[0])
    A = [[sum(w[i] * X[i][a] * X[i][b] for i in range(len(X))) + (1e-3 if a == b and a > 0 else 0) for b in range(n)] for a in range(n)]
    bb = [sum(w[i] * X[i][a] * y[i] for i in range(len(X))) for a in range(n)]
    return _resolver(A, bb)

def explicar(beta, d_obj, refs, coste_obj, coste_ref, ratio_ref):
    """Contribuciones en €/MWh (precio medio diario) frente a la media de los días de referencia."""
    v = _vars_expl(d_obj)
    vr = {k: media([_vars_expl(r)[k] for r in refs]) for k, _ in VARS_EXPL}
    out = []
    for i, (k, etiqueta) in enumerate(VARS_EXPL, start=1):
        out.append({"factor": etiqueta, "eur_mwh": round(beta[i] * (v[k] - vr[k]) * coste_obj, 1),
                    "valor": round(v[k], 2), "referencia": round(vr[k], 2)})
    out.append({"factor": "Gas y CO2", "eur_mwh": round(ratio_ref * (coste_obj - coste_ref), 1),
                "valor": round(coste_obj, 1), "referencia": round(coste_ref, 1)})
    return out

# ============================================================================
# Semáforo
# ============================================================================
def semaforo(pr, coste, precio_ref):
    p50 = pr["dia"][1]
    valle_p10 = pr["franjas"]["valle_solar"][0]
    cero = valle_p10 is not None and valle_p10 <= 1.0
    if p50 < 0.6 * precio_ref:
        nivel = "barato"
    elif p50 > 1.2 * precio_ref:
        nivel = "caro"
    else:
        nivel = "medio"
    return {"nivel": nivel, "riesgo_cero": cero}
