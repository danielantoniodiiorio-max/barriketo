import os
import sys
import json
import logging
from datetime import datetime, date, timedelta
import requests
from dotenv import load_dotenv
from garminconnect import (
    Garmin,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
    GarminConnectAuthenticationError,
)

# Configuración de logs
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("BarriketoGarminSync")

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(ENV_PATH)

DEFAULT_WEBHOOK_URL = "https://script.google.com/macros/s/AKfycbwgwwUth2hPPWgdOJiud4aP9rCTLimhkgdAQnv5fo46htDnxWW78BX5QClFJOEK3as-/exec"
GARMIN_EMAIL = os.getenv("GARMIN_EMAIL")
GARMIN_PASSWORD = os.getenv("GARMIN_PASSWORD")
GARMIN_TOKENS_ENV = os.getenv("GARMIN_TOKENS")
WEBHOOK_URL = os.getenv("BARRIKETO_WEBHOOK_URL") or DEFAULT_WEBHOOK_URL
TOKEN_DIR = os.getenv("GARMIN_TOKENS_DIR", os.path.expanduser("~/.garminconnect"))
SECRET_TOKEN = os.getenv("BARRIKETO_SECRET_TOKEN", "barriketo_secure_2026")

def init_garmin_client():
    """Inicia sesión en Garmin Connect reutilizando tokens persistidos o inyectados desde Cloud Secrets."""
    os.makedirs(TOKEN_DIR, exist_ok=True)
    token_file = os.path.join(TOKEN_DIR, "garmin_tokens.json")

    if not os.path.exists(token_file) and GARMIN_TOKENS_ENV:
        try:
            with open(token_file, "w", encoding="utf-8") as tf:
                tf.write(GARMIN_TOKENS_ENV.strip())
            logger.info("Tokens iniciales de Garmin restaurados desde secreto en la nube.")
        except Exception as e:
            logger.warning(f"No se pudo escribir GARMIN_TOKENS_ENV: {e}")

    if not os.path.exists(token_file) and (not GARMIN_EMAIL or not GARMIN_PASSWORD):
        logger.error("Faltan credenciales GARMIN_EMAIL / GARMIN_PASSWORD o GARMIN_TOKENS.")
        sys.exit(1)

    try:
        garmin = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
        garmin.login(tokenstore=TOKEN_DIR)
        try:
            garmin.client.dump(TOKEN_DIR)
        except Exception:
            pass
        logger.info(f"Sesión establecida con éxito en Garmin Connect ({garmin.display_name}).")
        return garmin
    except Exception as err:
        logger.error(f"Error autenticando con Garmin Connect: {err}")
        sys.exit(1)

def extract_day_metrics(garmin, target_date: date):
    """Obtiene y procesa las métricas de un día específico, incluyendo actividades de fuerza."""
    date_str = target_date.isoformat()
    display_date = date_str
    logger.info(f"Consultando métricas de Garmin para {display_date}...")

    try:
        user_summary = garmin.get_user_summary(date_str)
    except Exception as err:
        logger.error(f"Error al obtener user summary de Garmin para {date_str}: {err}")
        return None

    # Extracción de campos generales de Garmin
    pasos = user_summary.get("totalSteps", 0) or 0
    mod_int = user_summary.get("moderateIntensityMinutes", 0) or 0
    vig_int = user_summary.get("vigorousIntensityMinutes", 0) or 0
    min_int = mod_int + (vig_int * 2)

    fc_rep = user_summary.get("restingHeartRate", 0) or 0
    cal_act = user_summary.get("activeKilocalories", 0) or 0
    cal_rep = user_summary.get("bmrKilocalories", 0) or 0
    cal_quem = user_summary.get("totalKilocalories", 0) or (cal_act + cal_rep)
    pisos = user_summary.get("floorsAscended", 0) or 0

    # EXTRACCIÓN DE SESIONES DE FUERZA DETALLADAS REGISTRADAS EN GARMIN
    fuerza_min = 0.0
    cal_fuerza = 0.0
    try:
        activities = garmin.get_activities_by_date(date_str, date_str) or []
        for a in activities:
            type_key = a.get("activityType", {}).get("typeKey", "")
            name = a.get("activityName", "").lower()
            if "strength" in type_key or "fuerza" in name:
                dur = (a.get("duration") or 0) / 60.0
                cals = a.get("calories") or 0.0
                fuerza_min += dur
                cal_fuerza += cals
        fuerza_min = round(fuerza_min, 1)
        cal_fuerza = round(cal_fuerza, 1)
        logger.info(f"Actividades de fuerza encontradas para {date_str}: {fuerza_min} min | {cal_fuerza} kcal")
    except Exception as act_err:
        logger.warning(f"No se pudieron consultar actividades para {date_str}: {act_err}")

    # Glucógeno estimado consumido en fuerza (~70% del gasto anaeróbico de fuerza proviene de glucólisis / 4 kcal/g)
    glucogeno_est = round((cal_fuerza * 0.70 / 4), 1) if cal_fuerza > 0 else 0.0

    metrics = {
        "action": "sync_garmin",
        "token": SECRET_TOKEN,
        "fecha": display_date,
        "pasos": int(pasos),
        "minInt": int(min_int),
        "fcRep": int(fc_rep),
        "calAct": int(cal_act),
        "calRep": int(cal_rep),
        "calQuem": int(cal_quem),
        "pisos": int(pisos),
        "fuerzaMin": float(fuerza_min),
        "calFuerza": float(cal_fuerza),
        "glucogeno": float(glucogeno_est)
    }

    logger.info(f"Métricas extraídas: Pasos={pasos:,} | FC Rep={fc_rep} ppm | Cal Quemadas={cal_quem} kcal | Fuerza={fuerza_min} min ({cal_fuerza} kcal)")
    return metrics


def save_local_live_metrics(metrics):
    """Guarda las métricas de Garmin (incluyendo sesiones de fuerza) en garmin_live_metrics.js para lectura directa sin CORS desde el Dashboard."""
    try:
        eco_dir = os.path.dirname(os.path.abspath(__file__))
        desktop_dir = os.path.dirname(eco_dir)
        json_store_path = os.path.join(eco_dir, "garmin_live_metrics.json")

        store = {}
        if os.path.exists(json_store_path):
            try:
                with open(json_store_path, "r", encoding="utf-8") as jf:
                    store = json.load(jf)
            except Exception:
                store = {}

        # Clave DD/MM (ej: '28/10')
        parts = str(metrics.get("fecha", "")).split("-")
        if len(parts) == 3:
            day_key = f"{parts[2]}/{parts[1]}"
        else:
            day_key = str(metrics.get("fecha", ""))[:5]

        prev_entry = store.get(day_key, {})
        prev_sent_pasos = prev_entry.get("last_sent_pasos", 0)
        prev_sent_calQuem = prev_entry.get("last_sent_calQuem", 0)
        prev_sent_fuerza = prev_entry.get("last_sent_fuerzaMin", 0)

        cur_pasos = metrics.get("pasos", 0)
        cur_calQuem = metrics.get("calQuem", 0)
        cur_fuerza = metrics.get("fuerzaMin", 0)

        # Detectar si las métricas cambiaron respecto al último envío exitoso
        metrics_changed = (
            cur_pasos != prev_sent_pasos or
            abs(cur_calQuem - prev_sent_calQuem) >= 10 or
            cur_fuerza != prev_sent_fuerza
        )

        # Preservar el historial de último envío
        metrics["last_sent_pasos"] = prev_sent_pasos
        metrics["last_sent_calQuem"] = prev_sent_calQuem
        metrics["last_sent_fuerzaMin"] = prev_sent_fuerza
        store[day_key] = metrics

        with open(json_store_path, "w", encoding="utf-8") as jf:
            json.dump(store, jf, indent=2, ensure_ascii=False)

        js_payload = f"window.GARMIN_LIVE_METRICS = {json.dumps(store, ensure_ascii=False)};\n"
        for target_dir in [eco_dir, desktop_dir, os.path.join(desktop_dir, "Barriketo_Netlify_Deploy")]:
            if os.path.exists(target_dir):
                js_path = os.path.join(target_dir, "garmin_live_metrics.js")
                with open(js_path, "w", encoding="utf-8") as jsf:
                    jsf.write(js_payload)
        logger.info(f"Archivo local garmin_live_metrics.js actualizado para {day_key} (Pasos: {cur_pasos:,} | Fuerza: {cur_fuerza} min).")
        return metrics_changed
    except Exception as e:
        logger.warning(f"No se pudo guardar garmin_live_metrics.js local: {e}")
        return False

def record_successful_sheets_sync(metrics):
    """Registra que las métricas actuales se enviaron con éxito a Google Sheets para no reenviarlas si no cambian."""
    try:
        eco_dir = os.path.dirname(os.path.abspath(__file__))
        json_store_path = os.path.join(eco_dir, "garmin_live_metrics.json")
        if not os.path.exists(json_store_path):
            return

        with open(json_store_path, "r", encoding="utf-8") as jf:
            store = json.load(jf)

        parts = str(metrics.get("fecha", "")).split("-")
        day_key = f"{parts[2]}/{parts[1]}" if len(parts) == 3 else str(metrics.get("fecha", ""))[:5]

        if day_key in store:
            store[day_key]["last_sent_pasos"] = metrics.get("pasos", 0)
            store[day_key]["last_sent_calQuem"] = metrics.get("calQuem", 0)
            store[day_key]["last_sent_fuerzaMin"] = metrics.get("fuerzaMin", 0)
            with open(json_store_path, "w", encoding="utf-8") as jf:
                json.dump(store, jf, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"No se pudo registrar éxito de Sheets sync: {e}")

def send_to_google_sheets(metrics):
    """Envía las métricas al Webhook de Google Apps Script."""
    if not WEBHOOK_URL:
        logger.warning("No se definió BARRIKETO_WEBHOOK_URL. Imprimiendo payload JSON localmente:")
        print(json.dumps(metrics, indent=2))
        return False

    try:
        resp = requests.post(
            WEBHOOK_URL,
            json=metrics,
            headers={"Content-Type": "application/json"},
            timeout=15
        )
        if resp.status_code == 200:
            logger.info(f"Sincronización exitosa con Google Sheets ({resp.status_code}): {resp.text}")
            record_successful_sheets_sync(metrics)
            return True
        else:
            logger.error(f"Error al enviar a Google Sheets ({resp.status_code}): {resp.text}")
            return False
    except Exception as err:
        logger.error(f"Fallo de conexión al enviar webhook a Google Sheets: {err}")
        return False

def sync_today():
    """Sincroniza los últimos 5 días hasta HOY para recuperar cualquier día en que la PC haya estado apagada."""
    logger.info("Iniciando sincronización de Garmin (últimos 5 días hasta HOY)...")
    garmin = init_garmin_client()
    today = date.today()
    for days_back in range(4, -1, -1):
        target_d = today - timedelta(days=days_back)
        metrics = extract_day_metrics(garmin, target_d)
        if metrics:
            should_sync_to_sheets = save_local_live_metrics(metrics)
            if should_sync_to_sheets:
                send_to_google_sheets(metrics)

def sync_range(start_date: date, end_date: date):
    """Sincroniza un rango histórico de fechas."""
    logger.info(f"Iniciando sincronización de Garmin desde {start_date} hasta {end_date}...")
    garmin = init_garmin_client()
    curr = start_date
    while curr <= end_date:
        metrics = extract_day_metrics(garmin, curr)
        if metrics:
            should_sync_to_sheets = save_local_live_metrics(metrics)
            if should_sync_to_sheets:
                send_to_google_sheets(metrics)
        curr += timedelta(days=1)

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--history":
        start_d = date(2026, 10, 21)
        end_d = date.today()
        sync_range(start_d, end_d)
    else:
        sync_today()
