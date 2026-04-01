import os
import re
import json
import base64
import requests
from openai import OpenAI
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

# Configurar OpenRouter
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
client = None
if OPENROUTER_API_KEY:
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=OPENROUTER_API_KEY,
    )

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
    start_iso: str = ""
    end_iso: str = ""

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
        put_res = requests.put(
            f"{GCAL_BASE}/{event_id}",
            headers=headers,
            json=payload,
        )
        if put_res.status_code != 200:
            print(f"GCAL UPDATE ERROR [{put_res.status_code}]: {put_res.text}")
        return "updated" if put_res.status_code == 200 else "error"

    elif get_res.status_code == 404:
        payload["id"] = event_id
        post_res = requests.post(GCAL_BASE, headers=headers, json=payload)
        if post_res.status_code != 200:
            print(f"GCAL INSERT ERROR [{post_res.status_code}]: {post_res.text}")
        return "created" if post_res.status_code == 200 else "error"

    else:
        print(f"GCAL GET ERROR [{get_res.status_code}]: {get_res.text}")
        return "error"


def find_first_weekday(start_date: datetime, target_weekday: int) -> datetime:
    """Encuentra la primera fecha >= start_date cuyo weekday() == target_weekday."""
    days_ahead = target_weekday - start_date.weekday()
    if days_ahead < 0:
        days_ahead += 7
    return start_date + timedelta(days=days_ahead)


def generate_task_gcal_id(fecha: str, bloque_id: str) -> str:
    """
    Genera un ID determinista para eventos individuales (tareas) en Google Calendar.
    Usa fecha + bloque_id como semilla → base32hex (a-v, 0-9).
    """
    fecha_limpia = re.sub(r"[^0-9]", "", fecha)
    bloque_limpio = re.sub(r"[^a-z0-9]", "", bloque_id.lower())
    seed = f"tarea{fecha_limpia}{bloque_limpio}"
    encoded = base64.b32hexencode(seed.encode()).decode().lower().rstrip("=")
    return encoded


# =============================================
# ENDPOINTS
# =============================================

# 1. Obtener tareas de una fecha (Semanas futuras)
@app.get("/api/tareas/{fecha}")
def obtener_tareas(fecha: str):
    res = supabase.table("tareas").select("*").eq("fecha", fecha).execute()
    exc = supabase.table("excepciones").select("bloque_id").eq("fecha", fecha).execute()
    return {"data": res.data, "excepciones": [r["bloque_id"] for r in exc.data]}

# 2. Guardar o actualizar tarea en Supabase + Google Calendar
@app.post("/api/tareas")
def guardar_tarea(tarea: Tarea, request: Request):
    # Supabase: upsert (solo campos que existen en la tabla)
    db_data = {"fecha": tarea.fecha, "bloque_id": tarea.bloque_id, "descripcion": tarea.descripcion}
    existe = supabase.table("tareas").select("id").eq("fecha", tarea.fecha).eq("bloque_id", tarea.bloque_id).execute()
    if len(existe.data) > 0:
        supabase.table("tareas").update({"descripcion": tarea.descripcion}).eq("id", existe.data[0]["id"]).execute()
    else:
        supabase.table("tareas").insert([db_data]).execute()

    # Google Calendar: sync si hay token
    gcal_synced = False
    gcal_error = ""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        gcal_headers = {"Authorization": auth_header, "Content-Type": "application/json"}
        gcal_id = generate_task_gcal_id(tarea.fecha, tarea.bloque_id)
        print(f"GCAL SYNC: id={gcal_id}, start_iso={tarea.start_iso}, end_iso={tarea.end_iso}")

        if tarea.start_iso and tarea.end_iso:
            gcal_payload = {
                "summary": f"📘 {tarea.descripcion[:80]}",
                "description": tarea.descripcion,
                "start": {"dateTime": tarea.start_iso, "timeZone": "America/Guayaquil"},
                "end": {"dateTime": tarea.end_iso, "timeZone": "America/Guayaquil"},
            }
            try:
                result = upsert_event(gcal_headers, gcal_id, gcal_payload)
                gcal_synced = result in ("created", "updated")
                if not gcal_synced:
                    gcal_error = f"upsert returned: {result}"
            except Exception as e:
                gcal_error = str(e)
                print(f"ERROR GOOGLE CALENDAR: {gcal_error}")
        else:
            gcal_error = "No start_iso/end_iso provided"
            print(f"GCAL SKIP: {gcal_error}")

    return {"status": "success", "gcal_synced": gcal_synced, "gcal_error": gcal_error}

# 2b. Borrar tarea: físico para manuales, lógico para estáticos + Google Calendar
@app.delete("/api/tareas/{fecha}/{bloque_id}")
def borrar_tarea(fecha: str, bloque_id: str, request: Request):
    auth_header = request.headers.get("Authorization", "")
    gcal_headers = None
    if auth_header.startswith("Bearer "):
        gcal_headers = {"Authorization": auth_header, "Content-Type": "application/json"}

    deleted_count = 0

    if bloque_id.startswith("work_") or bloque_id.startswith("research_"):
        # LÓGICO: bloque estático → guardar excepción
        existe = supabase.table("excepciones").select("id").eq("fecha", fecha).eq("bloque_id", bloque_id).execute()
        if not existe.data:
            supabase.table("excepciones").insert([{"fecha": fecha, "bloque_id": bloque_id}]).execute()
        supabase.table("tareas").delete().eq("fecha", fecha).eq("bloque_id", bloque_id).execute()
        deleted_count = 1
    else:
        # FÍSICO: custom → borrar registro de Supabase
        resultado = supabase.table("tareas").delete().eq("fecha", fecha).eq("bloque_id", bloque_id).execute()
        deleted_count = len(resultado.data) if resultado.data else 0

    # Google Calendar: borrar con ID determinista unificado
    gcal_deleted = False
    if gcal_headers:
        gcal_id = generate_task_gcal_id(fecha, bloque_id)
        try:
            r = requests.delete(f"{GCAL_BASE}/{gcal_id}", headers=gcal_headers)
            gcal_deleted = r.status_code in (200, 204, 410)
        except Exception:
            pass

    return {"status": "success", "deleted": deleted_count, "gcal_deleted": gcal_deleted}

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

# === AGENTE IA: Planificación Semanal con Gemini ===

class PlanIA(BaseModel):
    prompt_usuario: str
    fecha_inicio_semana: str  # YYYY-MM-DD (lunes)
    token_google: str = ""

@app.post("/api/planificar-semana-ia")
def planificar_semana_ia(plan: PlanIA, request: Request):
    if not client:
        raise HTTPException(status_code=500, detail="OPENROUTER_API_KEY no configurada")

    # Calcular horas libres Lun-Vie (07:00-12:00 siempre libre, 12:00-13:00 almuerzo)
    fecha_lunes = datetime.strptime(plan.fecha_inicio_semana, "%Y-%m-%d")
    horas_libres = []
    for day_offset in range(5):  # Lun a Vie
        d = fecha_lunes + timedelta(days=day_offset)
        ds = d.strftime("%Y-%m-%d")
        dia_nombre = DAY_NAMES[d.weekday()]
        occupied = set(SCHEDULE_DATA.get(d.weekday(), {}).keys())
        # Bloques de mañana (07:00-12:00) siempre libres
        for h in range(7, 12):
            hora = f"{h:02d}:00"
            horas_libres.append({"dia": ds, "dia_nombre": dia_nombre, "hora_inicio": hora, "hora_fin": f"{h+1:02d}:00"})
        # Bloques de tarde/noche (21:00-23:00) — siempre libre
        for h in range(21, 23):
            hora = f"{h:02d}:00"
            horas_libres.append({"dia": ds, "dia_nombre": dia_nombre, "hora_inicio": hora, "hora_fin": f"{h+1:02d}:00"})

    # Prompt estricto para Gemini
    system_prompt = f"""Eres un asistente de productividad académica para un profesor universitario que cursa un doctorado.
El usuario tiene estas metas para la semana: {plan.prompt_usuario}

Distribuye estas metas ÚNICAMENTE en las siguientes horas libres disponibles:
{json.dumps(horas_libres, ensure_ascii=False, indent=2)}

REGLAS ESTRICTAS:
1. Cada tarea ocupa exactamente UNO de los bloques listados (1 hora).
2. No inventes bloques nuevos; solo usa los del listado.
3. Distribuye las tareas de forma balanceada entre los días.
4. Asigna entre 3 y 8 bloques en total según la complejidad de las metas.
5. Devuelve ÚNICAMENTE un JSON válido: un arreglo de objetos.
6. Cada objeto: {{"dia": "YYYY-MM-DD", "hora_inicio": "HH:MM", "hora_fin": "HH:MM", "titulo": "resumen corto", "descripcion": "detalle de la tarea"}}
7. NO incluyas backticks, markdown, ni texto adicional. SOLO el JSON."""

    try:
        response = client.chat.completions.create(
            model="google/gemini-2.0-flash-exp:free",
            max_tokens=2000,
            messages=[
                {"role": "system", "content": "Eres un asistente de productividad. Devuelve ÚNICAMENTE un JSON válido con un arreglo de objetos que contengan: dia (YYYY-MM-DD), hora_inicio (HH:MM), hora_fin (HH:MM), titulo, descripcion. No uses backticks de markdown."},
                {"role": "user", "content": f"Metas: {plan.prompt_usuario}\nHoras libres: {json.dumps(horas_libres, ensure_ascii=False)}"}
            ]
        )
        raw_text = response.choices[0].message.content.strip()
        # Limpiar backticks de markdown
        if raw_text.startswith("```"):
            raw_text = re.sub(r'^```(?:json)?\s*', '', raw_text)
            raw_text = re.sub(r'\s*```$', '', raw_text)
        print(f"OPENROUTER RAW: {raw_text[:500]}")
        tareas_ia = json.loads(raw_text)
    except json.JSONDecodeError as e:
        print(f"JSON ERROR: {e}\nRAW: {raw_text[:500]}")
        raise HTTPException(status_code=422, detail=f"LLM devolvió JSON inválido: {str(e)}")
    except Exception as e:
        print(f"GEMINI ERROR: {e}")
        raise HTTPException(status_code=500, detail=f"Error de Gemini: {str(e)}")

    # Guardar en Supabase + Google Calendar
    auth_header = request.headers.get("Authorization", "")
    gcal_headers = None
    if auth_header.startswith("Bearer "):
        gcal_headers = {"Authorization": auth_header, "Content-Type": "application/json"}

    guardadas = 0
    for t in tareas_ia:
        bloque_id = f"ia_{t['dia'].replace('-','')}{t['hora_inicio'].replace(':','')}"
        db_data = {
            "fecha": t["dia"],
            "bloque_id": bloque_id,
            "descripcion": f"🤖 {t['titulo']}\n{t['descripcion']}"
        }
        # Upsert en Supabase
        existe = supabase.table("tareas").select("id").eq("fecha", t["dia"]).eq("bloque_id", bloque_id).execute()
        if existe.data:
            supabase.table("tareas").update({"descripcion": db_data["descripcion"]}).eq("id", existe.data[0]["id"]).execute()
        else:
            supabase.table("tareas").insert([db_data]).execute()
        guardadas += 1

        # Google Calendar sync
        if gcal_headers:
            gcal_id = generate_task_gcal_id(t["dia"], bloque_id)
            start_iso = f"{t['dia']}T{t['hora_inicio']}:00"
            end_iso = f"{t['dia']}T{t['hora_fin']}:00"
            gcal_payload = {
                "summary": f"🤖 {t['titulo']}",
                "description": t["descripcion"],
                "start": {"dateTime": start_iso, "timeZone": "America/Guayaquil"},
                "end": {"dateTime": end_iso, "timeZone": "America/Guayaquil"},
            }
            try:
                upsert_event(gcal_headers, gcal_id, gcal_payload)
            except Exception as e:
                print(f"GCAL IA ERROR: {e}")

    return {"status": "success", "tareas_generadas": guardadas, "detalle": tareas_ia}

# Servir el Frontend estático
app.mount("/", StaticFiles(directory="app/static", html=True), name="static")