import os
from celery import Celery

# Obtener la URL de Redis desde las variables de entorno
redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")

# Crear la aplicación Celery
celery = Celery(
    'recibos',
    broker=redis_url,      # Dónde están las tareas pendientes
    backend=redis_url      # Dónde guardar los resultados
)

# Configuración
celery.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='America/Argentina/Buenos_Aires',
    enable_utc=True,
    task_track_started=True,
    task_time_limit=3600,  # 1 hora máximo por tarea
)