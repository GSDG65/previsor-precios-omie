# -*- coding: utf-8 -*-
"""resumen_movil.py · Envía al móvil (app ntfy) un resumen de la previsión recién emitida.
Necesita la variable de entorno NTFY_TOPIC (nombre del canal en ntfy.sh). Opcional: PAGINA (enlace al visor)."""
import json, os, urllib.request, urllib.parse, datetime as dt

BASE = os.path.dirname(os.path.abspath(__file__))
DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
NIVEL = {"barato": "barato", "medio": "medio", "caro": "caro"}

def fmt(x, d=0):
    return f"{x:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")

def main():
    tema = os.environ.get("NTFY_TOPIC", "").strip()
    if not tema:
        print("sin NTFY_TOPIC: no se envía aviso"); return
    with open(os.path.join(BASE, "datos", "previsor_datos.js"), encoding="utf-8") as f:
        s = f.read()
    d = json.loads(s[s.index("{"): s.rindex("}") + 1])
    p = d.get("prevision")
    if not p:
        print("sin previsión"); return
    lineas = []
    for x in p["dias"]:
        f = dt.date.fromisoformat(x["fecha"])
        sem = x["semaforo"]
        linea = (f"{DIAS[f.weekday()]} {f.day}/{f.month}: {fmt(x['dia'][1])} €/MWh "
                 f"[{fmt(x['dia'][0])}–{fmt(x['dia'][2])}] {NIVEL.get(sem['nivel'], sem['nivel'])}"
                 + (" · riesgo 0" if sem.get("riesgo_cero") else ""))
        q = x.get("real_cuartos")
        r = q if q and len(q) == 96 else x.get("real_horas")
        if r:
            linea += f" · REAL media {fmt(sum(r) / len(r), 2)}, máx {fmt(max(r), 2)}, mín {fmt(min(r), 2)}"
        lineas.append(linea)
    # horas más baratas y más caras de mañana (real si ya está publicado; si no, previsión P50)
    m = p["dias"][0]
    q = m.get("real_cuartos")
    if q and len(q) == 96:
        horas = [sum(q[4 * h: 4 * h + 4]) / 4 for h in range(24)]; fuente = "real"
    else:
        horas = m.get("real_horas") or m["horas"]["p50"]; fuente = "real" if m.get("real_horas") else "previsto"
    orden = sorted(range(24), key=lambda h: horas[h])
    lineas.append(f"Mañana ({fuente}) más baratas: " + ", ".join(f"{h}h" for h in sorted(orden[:3]))
                  + " · más caras: " + ", ".join(f"{h}h" for h in sorted(orden[-3:])))
    if os.environ.get("PAGINA"):
        lineas.append(os.environ["PAGINA"])
    titulo = f"Precio luz: mañana {fmt(m['dia'][1])} €/MWh ({m['semaforo']['nivel']})"
    url = f"https://ntfy.sh/{tema}?" + urllib.parse.urlencode({"title": titulo, "tags": "zap"})
    req = urllib.request.Request(url, data="\n".join(lineas).encode("utf-8"))
    if os.environ.get("PAGINA"):
        req.add_header("Click", os.environ["PAGINA"])
    urllib.request.urlopen(req, timeout=30).read()
    print("aviso enviado:", titulo)

if __name__ == "__main__":
    main()
