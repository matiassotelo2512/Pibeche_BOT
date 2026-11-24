import datetime
import json
import os
import time
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai.errors import APIError
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from pydantic import BaseModel, Field
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
DEFAULT_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")
GOOGLE_TOKEN_FILE = os.getenv("GOOGLE_TOKEN_FILE", "token.json")
STATE_FILE = "bot_state.json"
TZ = ZoneInfo("America/Argentina/Buenos_Aires")

SCOPES = ["https://www.googleapis.com/auth/calendar"]

MENSAJE_AYUDA = (
    "👋 *Asistente de Agenda*\n\n"
    "Mandame un mensaje en lenguaje natural con lo que quieras agendar. "
    "Si incluís *fecha, hora y lugar*, mejor.\n\n"
    "*Ejemplos:*\n"
    "• `Mañana a las 15 tengo turno con el médico en Palermo`\n"
    "• `El viernes a las 19:30 reunión en Av. Corrientes 1234, dura 2 horas`\n\n"
    "*Comandos:*\n"
    "• `/start` o `/help` — Ayuda y ejemplos\n"
    "• `/hoy` — Ver eventos de hoy (todos tus calendarios)\n"
    "• `/semana` — Ver eventos de los próximos 7 días (todos tus calendarios)\n"
    "• `/calendarios` — Elegir en qué calendario se crean los eventos nuevos"
)


class EventoExtraido(BaseModel):
    titulo: str = Field(description="Nombre corto del evento, ej. Dentista")
    inicio_iso: str = Field(
        description="Fecha y hora de inicio en formato ISO 8601 con zona horaria, ej. 2026-11-25T16:00:00-03:00"
    )
    fin_iso: str = Field(
        description="Fecha y hora de fin en formato ISO 8601 con zona horaria (si no se indica duracion, sumar 1 hora)"
    )
    ubicacion: str | None = Field(default=None, description="Direccion o lugar si se menciona")


def cargar_calendario_activo() -> str:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                contenido = f.read().strip()
                if contenido:
                    data = json.loads(contenido)
                    if data.get("calendar_id"):
                        return data["calendar_id"]
        except Exception:
            pass
    return DEFAULT_CALENDAR_ID


def guardar_calendario_activo(calendar_id: str) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"calendar_id": calendar_id}, f, ensure_ascii=False, indent=2)


def get_calendar_service(token_file: str = GOOGLE_TOKEN_FILE):
    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_file, "w") as token:
            token.write(creds.to_json())

    return build("calendar", "v3", credentials=creds)


def listar_calendarios_escritura(service) -> list[dict]:
    items = service.calendarList().list().execute().get("items", [])
    return [
        {"id": item["id"], "summary": item.get("summary", item["id"])}
        for item in items
        if item.get("accessRole") in ("owner", "writer")
    ]


def listar_todos_los_calendarios(service) -> list[dict]:
    items = service.calendarList().list().execute().get("items", [])
    return [
        {"id": item["id"], "summary": item.get("summary", item["id"])}
        for item in items
    ]


def obtener_eventos_todos_calendarios(
    service, inicio: datetime.datetime, fin: datetime.datetime
) -> list[dict]:
    calendarios = listar_todos_los_calendarios(service)
    todos = []

    for cal in calendarios:
        try:
            res = (
                service.events()
                .list(
                    calendarId=cal["id"],
                    timeMin=inicio.isoformat(),
                    timeMax=fin.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )
            for ev in res.get("items", []):
                ev["_calendar_name"] = cal["summary"]
                todos.append(ev)
        except Exception:
            # Si algún calendario externo o de feriados falla al leer, seguimos con los demás
            continue

    def clave_orden(ev: dict) -> datetime.datetime:
        start_raw = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date", "")
        try:
            if "T" in start_raw:
                return datetime.datetime.fromisoformat(start_raw).astimezone(TZ)
            dt_fecha = datetime.date.fromisoformat(start_raw)
            return datetime.datetime.combine(dt_fecha, datetime.time.min, tzinfo=TZ)
        except Exception:
            return datetime.datetime.max.replace(tzinfo=TZ)

    todos.sort(key=clave_orden)
    return todos


def formatear_lista_eventos(eventos: list[dict], incluir_fecha: bool = False) -> str:
    if not eventos:
        return "No tenés eventos agendados en este rango."

    lineas = []
    for ev in eventos:
        titulo = ev.get("summary", "(Sin título)")
        cal_nombre = ev.get("_calendar_name", "")
        start_raw = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date", "")
        ubicacion = ev.get("location")

        hora_texto = start_raw
        try:
            if "T" in start_raw:
                dt = datetime.datetime.fromisoformat(start_raw).astimezone(TZ)
                hora_texto = dt.strftime("%d/%m %H:%M") if incluir_fecha else dt.strftime("%H:%M")
            else:
                dt_fecha = datetime.date.fromisoformat(start_raw)
                fecha_fmt = dt_fecha.strftime("%d/%m") if incluir_fecha else "Hoy"
                hora_texto = f"{fecha_fmt} (todo el día)"
        except ValueError:
            pass

        etiqueta_cal = f" `[{cal_nombre}]`" if cal_nombre else ""
        linea = f"• *{hora_texto}* — {titulo}{etiqueta_cal}"
        if ubicacion:
            linea += f" _({ubicacion})_"
        lineas.append(linea)

    return "\n".join(lineas)


MODELOS_CANDIDATOS = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.5-flash-lite",
]


def interpretar_evento(texto_usuario: str, fecha_actual_iso: str) -> EventoExtraido:
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = (
        f"Fecha y hora actual de referencia: {fecha_actual_iso} (zona horaria Argentina -03:00).\n"
        f"Extrae los datos del evento a partir de este mensaje: '{texto_usuario}'.\n"
        "Si no se especifica hora de fin o duracion, asume 1 hora de duracion."
    )

    ultimo_error = None
    for modelo in MODELOS_CANDIDATOS:
        for intento in range(3):
            try:
                response = client.models.generate_content(
                    model=modelo,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=EventoExtraido,
                    ),
                )
                return EventoExtraido.model_validate_json(response.text)
            except APIError as e:
                ultimo_error = e
                if e.code in (503, 429) and intento < 2:
                    time.sleep(2 * (intento + 1))
                    continue
                if e.code in (503, 404, 429):
                    break
                raise

    raise ultimo_error


def crear_evento_calendar(service, evento: EventoExtraido, calendar_id: str) -> str:
    body = {
        "summary": evento.titulo,
        "start": {"dateTime": evento.inicio_iso},
        "end": {"dateTime": evento.fin_iso},
    }
    if evento.ubicacion:
        body["location"] = evento.ubicacion

    creado = service.events().insert(calendarId=calendar_id, body=body).execute()
    return creado.get("htmlLink", "")


async def comando_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(MENSAJE_AYUDA, parse_mode="Markdown")


async def comando_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(MENSAJE_AYUDA, parse_mode="Markdown")


async def comando_hoy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    try:
        service = get_calendar_service()
        ahora = datetime.datetime.now(TZ)
        inicio_dia = ahora.replace(hour=0, minute=0, second=0, microsecond=0)
        fin_dia = ahora.replace(hour=23, minute=59, second=59, microsecond=999999)

        eventos = obtener_eventos_todos_calendarios(service, inicio_dia, fin_dia)
        cuerpo = formatear_lista_eventos(eventos, incluir_fecha=False)
        await update.message.reply_text(
            f"📅 *Agenda de hoy ({ahora.strftime('%d/%m/%Y')})*\n\n{cuerpo}",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"Error al consultar los eventos de hoy: {e}")


async def comando_semana(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    try:
        service = get_calendar_service()
        ahora = datetime.datetime.now(TZ)
        inicio = ahora.replace(hour=0, minute=0, second=0, microsecond=0)
        fin = inicio + datetime.timedelta(days=7)

        eventos = obtener_eventos_todos_calendarios(service, inicio, fin)
        cuerpo = formatear_lista_eventos(eventos, incluir_fecha=True)
        await update.message.reply_text(
            f"🗓️ *Próximos 7 días (todos los calendarios)*\n\n{cuerpo}",
            parse_mode="Markdown",
        )
    except Exception as e:
        await update.message.reply_text(f"Error al consultar la agenda semanal: {e}")


async def comando_calendarios(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    try:
        service = get_calendar_service()
        calendarios = listar_calendarios_escritura(service)
        if not calendarios:
            await update.message.reply_text("No encontré calendarios con permiso de escritura.")
            return

        actual = cargar_calendario_activo()
        context.user_data["calendarios_map"] = {
            str(i): cal["id"] for i, cal in enumerate(calendarios)
        }

        botones = []
        for i, cal in enumerate(calendarios):
            marca = "✅ " if cal["id"] == actual else ""
            botones.append([
                InlineKeyboardButton(
                    text=f"{marca}{cal['summary']}",
                    callback_data=f"set_cal:{i}",
                )
            ])

        await update.message.reply_text(
            "Elegí en qué calendario querés que se creen los eventos nuevos:",
            reply_markup=InlineKeyboardMarkup(botones),
        )
    except Exception as e:
        await update.message.reply_text(f"Error al listar calendarios: {e}")


async def manejar_callback_calendario(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.data:
        return
    await query.answer()

    if query.data.startswith("set_cal:"):
        idx = query.data.split(":", 1)[1]
        mapa = context.user_data.get("calendarios_map", {})
        cal_id = mapa.get(idx)
        if cal_id:
            guardar_calendario_activo(cal_id)
            await query.edit_message_text(f"Calendario de destino actualizado a:\n`{cal_id}`", parse_mode="Markdown")
        else:
            await query.edit_message_text("La selección expiró. Volvé a ejecutar /calendarios.")


async def manejar_mensaje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    texto = update.message.text.strip()
    await update.message.reply_text("Interpretando y agendando...")

    try:
        ahora_iso = datetime.datetime.now(TZ).isoformat()
        evento = interpretar_evento(texto, ahora_iso)
        service = get_calendar_service()
        cal_id = cargar_calendario_activo()
        link = crear_evento_calendar(service, evento, calendar_id=cal_id)

        respuesta = (
            f"Listo, agendé *{evento.titulo}*.\n"
            f"Inicio: `{evento.inicio_iso}`\n"
        )
        if evento.ubicacion:
            respuesta += f"Lugar: {evento.ubicacion}\n"
        if link:
            respuesta += f"[Ver en Google Calendar]({link})"

        await update.message.reply_text(respuesta, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Hubo un error al agendar: {e}")


def main():
    if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
        raise RuntimeError("Faltan TELEGRAM_TOKEN o GEMINI_API_KEY en el archivo .env")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", comando_start))
    app.add_handler(CommandHandler("help", comando_help))
    app.add_handler(CommandHandler("hoy", comando_hoy))
    app.add_handler(CommandHandler("semana", comando_semana))
    app.add_handler(CommandHandler("calendarios", comando_calendarios))
    app.add_handler(CallbackQueryHandler(manejar_callback_calendario))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, manejar_mensaje))

    print("Bot corriendo...")
    print("Comandos: /start, /help, /hoy, /semana, /calendarios")
    app.run_polling()


if __name__ == "__main__":
    main()