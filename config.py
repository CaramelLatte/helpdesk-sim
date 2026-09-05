import os
from dotenv import load_dotenv

load_dotenv()

ZAMMAD_URL = f"http://{os.getenv('ZAMMAD_HOST_ADDRESS', 'localhost')}:{os.getenv('ZAMMAD_PORT', '8080')}"
ZAMMAD_API_KEY = os.getenv('API_KEY')
OPERATOR_API_KEY = os.getenv('OPERATOR_API_KEY')

LM_STUDIO_URL = f"http://{os.getenv('LM_STUDIO_ADDRESS', 'localhost')}:{os.getenv('LM_STUDIO_PORT', '1234')}/v1"
PERSONA_MODEL = os.getenv('TICKET_GEN_MODEL', 'local-model')
GRADING_MODEL = os.getenv('GRADING_MODEL', 'local-model')

DB_PATH = os.getenv('DB_PATH', 'ai_ticket.db')
SCENARIOS_DIR = os.getenv('SCENARIOS_DIR', 'scenarios')

# How often the background job checks for new activity, in seconds.
SCHEDULER_INTERVAL_SECONDS = int(os.getenv('SCHEDULER_INTERVAL_SECONDS', '60'))

FLASK_PORT = int(os.getenv('FLASK_PORT', '5001'))
FLASK_HOST = os.getenv('FLASK_HOST', '127.0.0.1')

# Graded/closed tickets older than this many hours are automatically deleted.
# Set to 0 to disable auto-cleanup.
CLEANUP_AFTER_HOURS = int(os.getenv('CLEANUP_AFTER_HOURS', '24'))
