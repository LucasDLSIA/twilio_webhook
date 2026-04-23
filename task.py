from celery_app import celery
import time
import requests
import os

# URL de tu app
APP_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://twilio-webhook-lddc.onrender.com")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

@celery.task(name='tasks.process_queue_auto')
def process_queue_auto(tenant, period, batch_size=10):
    """
    Procesa la cola automáticamente llamando a queue_tick en loop.
    """
    processed_total = 0
    sent_total = 0
    iterations = 0
    max_iterations = 1000  # Límite de seguridad (10,000 envíos con batch=10)
    
    while iterations < max_iterations:
        iterations += 1
        
        # Llamar a tu endpoint actual
        try:
            response = requests.post(
                f"{APP_URL}/admin/send_template_queue_tick",
                data={
                    "tenant": tenant,
                    "period": period,
                    "batch_size": batch_size,
                    "mode": "json",
                    "token": ADMIN_TOKEN,
                },
                timeout=60
            )
            
            if response.status_code != 200:
                return {
                    "status": "error",
                    "message": f"Error HTTP {response.status_code}",
                    "processed": processed_total,
                    "sent": sent_total,
                    "iterations": iterations
                }
            
            data = response.json()
            processed = data.get("processed", 0)
            sent = data.get("sent", 0)
            
            processed_total += processed
            sent_total += sent
            
            # Si no procesó nada, ya terminamos
            if processed == 0:
                return {
                    "status": "completed",
                    "message": "No quedan más pendientes",
                    "processed": processed_total,
                    "sent": sent_total,
                    "iterations": iterations
                }
            
            # Pausa de 2 segundos antes del siguiente lote
            time.sleep(2)
            
        except Exception as e:
            return {
                "status": "error",
                "message": str(e),
                "processed": processed_total,
                "sent": sent_total,
                "iterations": iterations
            }
    
    return {
        "status": "max_iterations_reached",
        "message": f"Se alcanzó el límite de {max_iterations} iteraciones",
        "processed": processed_total,
        "sent": sent_total,
        "iterations": iterations
    }