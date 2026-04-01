import os
import re
import base64
import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv
from datetime import datetime, timedelta

load_dotenv()

# Configuración Supabase
url: str = os.environ.get("SUPABASE_URL")
key: str = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(url, key)

# Cargar claves de Google
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")

GCAL_BASE = "https://www.googleapis.com/calendar/v3/calendars/primary/events"

DAY_NAMES = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# === DISTRIBUTIVO DOCENTE ===
SCHEDULE_DATA = {
    0: {"13:00": "AD 5 - Tutorías", "14:00": "AD 5 - Tutorías", "15:00": "AD 1 - ACD Ecuaciones Diferenciales", "16:00": "AD 1 - ACD Ecuaciones Diferenciales", "17:00": "AD 1 - ACD Álgebra Lineal", "18:00": "AD 1 - ACD Álgebra Lineal", "19:00": "AV 1 - Vinculación", "20:00": "AV 1 - Vinculación"},
    1: {"13:00": "AG 5c - Consejo Consultivo", "14:00": "AD 1 - ACD Ecuaciones Diferenciales", "15:00": "AD 1 - ACD Ecuaciones Diferenciales", "16:00": "AD 1 - ACD Cálculo de una Variable", "17:00": "AD 1 - ACD Cálculo de una Variable", "18:00": "AD 1 - APE Cálculo de una Variable", "19:00": "AI 1 - Investigación", "20:00": "AI 1 - Investigación"},
    2: {"13:00": "AG 5c - Consejo Consultivo", "14:00": "AD 2 - Planificación", "15:00": "AD 2 - Planificación", "16:00": "AD 8 - Evaluación", "17:00": "AD 1 - ACD Cálculo de una Variable", "18:00": "AD 1 - ACD Cálculo de una Variable", "19:00": "AD 1 - APE Ecuaciones Diferenciales", "20:00": "AD 1 - APE Ecuaciones Diferenciales"},
    3: {"13:00": "AG 5c - Consejo Consultivo", "14:00": "AD 2 - Planificación", "15:00": "AD 2 - Planificación", "16:00": "AD 8 - Evaluación", "17:00": "AV 1 - Vinculación", "18:00": "AV 1 - Vinculación", "19:00": "AD 1 - APE Cálculo de una Variable", "20:00": "AD 1 - APE Cálculo de una Variable"},
    4: {"13:00": "AG 5c - Consejo Consultivo", "14:00": "AD 2 - Planificación", "15:00": "AD 9 - Dirección de Trabajos", "16:00": "AD 8 - Evaluación", "17:00": "AD 1 - ACD Álgebra Lineal", "18:00": "AD 1 - APE Álgebra Lineal", "19:00": "AI 1 - Investigación", "20:00": "AI 1 - Investigación"}
}

class Tarea(BaseModel):
    fecha: str
    bloque_id: str
    descripcion: str

class SyncCiclo(BaseModel):
    token: str
    fecha_inicio: str
    fecha_fin: str
    semestre_id: str


# =============================================
# HELPERS: Deterministic ID & Upsert
# =============================================

def generate_event_id(day_index: int, hora: str, semestre_id: str) -> str:
    """
    Genera un ID determinista compatible con Google Calendar.
    Google exige: caracteres [a-v0-9], longitud 5-1024.
    Estrategia: construir seed legible → codificar en base32hex lowercase.
    """
    dia = DAY_NAMES[day_index]
    hora_limpia = hora.replace(":", "")
    semestre_limpio = re.sub(r"[^a-z0-9]", "", semestre_id.lower())
    seed = f"clase{dia}{hora_limpia}{semestre_limpio}"
    # base32hex: A-V + 0-9  →  lowercase a-v + 0-9 (Google Calendar compatible)
    encoded = base64.b32hexencode(seed.encode()).decode().lower().rstrip("=")
    return encoded


def upsert_event(headers: dict, event_id: str, payload: dict) -> str:
    """
    GET el evento por ID.
      - 200 → PUT (update)
      - 404 → POST (insert con id fijado)
    Retorna 'created' | 'updated' | 'error'.
    """
    get_res = requests.get(f"{GCAL_BASE}/{event_id}", headers=headers)

    if get_res.status_code == 200:
        # El evento ya existe → actualizar la serie completa
        put_res = requests.put(
            f"{GCAL_BASE}/{event_id}",
            headers=headers,
            json=payload,
        )
        return "updated" if put_res.status_code == 200 else "error"

    elif get_res.status_code == 404:
        # No existe → crear con ID determinista
        payload["id"] = event_id
        post_res = requests.post(GCAL_BASE, headers=headers, json=payload)
        return "created" if post_res.status_code == 200 else "error"

    else:
        return "error"


def find_first_weekday(start_date: datetime, target_weekday: int) -> datetime:
    """Encuentra la primera fecha >= start_date cuyo weekday() == target_weekday."""
    days_ahead = target_weekday - start_date.weekday()
    if days_ahead < 0:
        days_ahead += 7
    return start_date + timedelta(days=days_ahead)


# =============================================
# ENDPOINTS
# =============================================

# 1. Obtener tareas de una fecha (Semanas futuras)
@app.get("/api/tareas/{fecha}")
def obtener_tareas(fecha: str):
    res = supabase.table("tareas").select("*").eq("fecha", fecha).execute()
    return {"data": res.data}

# 2. Guardar o actualizar tarea en Supabase
@app.post("/api/tareas")
def guardar_tarea(tarea: Tarea):
    existe = supabase.table("tareas").select("id").eq("fecha", tarea.fecha).eq("bloque_id", tarea.bloque_id).execute()
    if len(existe.data) > 0:
        supabase.table("tareas").update({"descripcion": tarea.descripcion}).eq("id", existe.data[0]["id"]).execute()
    else:
        supabase.table("tareas").insert([tarea.dict()]).execute()
    return {"status": "success"}

# 2b. Borrar tarea: físico para manuales/investigación, lógico para distributivo
@app.delete("/api/tareas/{fecha}/{bloque_id}")
def borrar_tarea(fecha: str, bloque_id: str, request: Request):
    auth_header = request.headers.get("Authorization", "")
    gcal_headers = None
    if auth_header.startswith("Bearer "):
        gcal_headers = {"Authorization": auth_header, "Content-Type": "application/json"}

    deleted_count = 0

    if bloque_id.startswith("work_"):
        # LÓGICO: clase del distributivo → guardar excepción (no borrar, es estática)
        existe = supabase.table("excepciones").select("id").eq("fecha", fecha).eq("bloque_id", bloque_id).execute()
        if not existe.data:
            supabase.table("excepciones").insert([{"fecha": fecha, "bloque_id": bloque_id}]).execute()
        # También borrar cualquier tarea asociada para ese bloque
        supabase.table("tareas").delete().eq("fecha", fecha).eq("bloque_id", bloque_id).execute()
        # Intentar borrar de Google Calendar
        if gcal_headers:
            try:
                parts = bloque_id.split("_")
                if len(parts) == 3:
                    day_idx = int(parts[1]) - 1
                    hora = parts[2][:2] + ":" + parts[2][2:]
                    for sem in ["2026a", "2026b"]:
                        event_id = generate_event_id(day_idx, hora, sem)
                        requests.delete(f"{GCAL_BASE}/{event_id}", headers=gcal_headers)
            except Exception:
                pass
        deleted_count = 1
    else:
        # FÍSICO: investigación o custom → borrar registro de Supabase
        registro = supabase.table("tareas").select("*").eq("fecha", fecha).eq("bloque_id", bloque_id).execute()
        google_event_id = None
        if registro.data and len(registro.data) > 0:
            google_event_id = registro.data[0].get("google_event_id")
        resultado = supabase.table("tareas").delete().eq("fecha", fecha).eq("bloque_id", bloque_id).execute()
        deleted_count = len(resultado.data) if resultado.data else 0
        if gcal_headers and google_event_id:
            try:
                requests.delete(f"{GCAL_BASE}/{google_event_id}", headers=gcal_headers)
            except Exception:
                pass

    return {"status": "success", "deleted": deleted_count}

# 2c. Obtener excepciones de una fecha (clases borradas lógicamente)
@app.get("/api/excepciones/{fecha}")
def obtener_excepciones(fecha: str):
    res = supabase.table("excepciones").select("bloque_id").eq("fecha", fecha).execute()
    return {"data": [r["bloque_id"] for r in res.data]}

@app.post("/api/sincronizar-semestre")
def sincronizar_semestre(req: SyncCiclo):
    headers = {
        "Authorization": f"Bearer {req.token}",
        "Content-Type": "application/json",
    }

    start_dt = datetime.strptime(req.fecha_inicio, "%Y-%m-%d")
    end_dt = datetime.strptime(req.fecha_fin, "%Y-%m-%d")
    rrule_until = end_dt.strftime("%Y%m%dT235959Z")

    creados = 0
    actualizados = 0
    errores = 0

    for dia_idx, clases_dia in SCHEDULE_DATA.items():
        # Fecha de la primera ocurrencia de este día de la semana
        first_date = find_first_weekday(start_dt, dia_idx)
        if first_date > end_dt:
            continue

        for hora_str, actividad in clases_dia.items():
            event_id = generate_event_id(dia_idx, hora_str, req.semestre_id)

            hora_inicio = datetime.strptime(hora_str, "%H:%M").time()
            dt_inicio = datetime.combine(first_date.date(), hora_inicio)
            dt_fin = dt_inicio + timedelta(hours=1)

            payload = {
                "summary": f"🏛️ {actividad}",
                "description": f"Carga automática - Distributivo Docente | Semestre: {req.semestre_id}",
                "start": {
                    "dateTime": dt_inicio.isoformat() + "-05:00",
                    "timeZone": "America/Guayaquil",
                },
                "end": {
                    "dateTime": dt_fin.isoformat() + "-05:00",
                    "timeZone": "America/Guayaquil",
                },
                "recurrence": [
                    f"RRULE:FREQ=WEEKLY;UNTIL={rrule_until}"
                ],
                "extendedProperties": {
                    "private": {
                        "origenApp": "agendaDoctoral",
                        "semestreId": req.semestre_id,
                        "diaIndex": str(dia_idx),
                        "horaInicio": hora_str,
                    }
                },
            }

            result = upsert_event(headers, event_id, payload)
            if result == "created":
                creados += 1
            elif result == "updated":
                actualizados += 1
            else:
                errores += 1

    return {
        "status": "success",
        "eventos_creados": creados,
        "eventos_actualizados": actualizados,
        "errores": errores,
    }

# 4. Leer eventos de Google Calendar para un día específico (Fase 1 bidireccional)
@app.get("/api/calendario/eventos/{fecha}")
def obtener_eventos_calendario(fecha: str, request: Request):
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token de autorización requerido")

    headers = {
        "Authorization": auth_header,
        "Content-Type": "application/json",
    }

    # Construir rango del día completo en America/Guayaquil (UTC-5)
    time_min = f"{fecha}T00:00:00-05:00"
    time_max = f"{fecha}T23:59:59-05:00"

    params = {
        "timeMin": time_min,
        "timeMax": time_max,
        "singleEvents": "true",
        "orderBy": "startTime",
    }

    res = requests.get(GCAL_BASE, headers=headers, params=params)

    if res.status_code != 200:
        raise HTTPException(
            status_code=res.status_code,
            detail=f"Error de Google Calendar: {res.text[:200]}"
        )

    items = res.json().get("items", [])
    eventos = []
    for item in items:
        start = item.get("start", {})
        end = item.get("end", {})
        # Soportar eventos de día completo (date) y eventos con hora (dateTime)
        hora_inicio = start.get("dateTime", start.get("date", ""))
        hora_fin = end.get("dateTime", end.get("date", ""))

        # Extraer solo HH:MM si es dateTime ISO
        if "T" in hora_inicio:
            hora_inicio = hora_inicio.split("T")[1][:5]
        if "T" in hora_fin:
            hora_fin = hora_fin.split("T")[1][:5]

        eventos.append({
            "titulo": item.get("summary", "(Sin título)"),
            "hora_inicio": hora_inicio,
            "hora_fin": hora_fin,
            "descripcion": item.get("description", ""),
            "origen_app": item.get("extendedProperties", {}).get("private", {}).get("origenApp", ""),
        })

    return {"data": eventos, "total": len(eventos)}

# Nuevo endpoint para enviar la configuración al frontend
@app.get("/api/config")
def obtener_configuracion():
    return {
        "GOOGLE_API_KEY": GOOGLE_API_KEY,
        "GOOGLE_CLIENT_ID": GOOGLE_CLIENT_ID
    }

# Servir el Frontend estático
app.mount("/", StaticFiles(directory="app/static", html=True), name="static")