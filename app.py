from flask import Flask
import db
import services.zammad as zammad
import services.scheduler as scheduler
from routes.tickets import tickets_bp
from routes.shell import shell_bp

app = Flask(__name__)
app.register_blueprint(tickets_bp)
app.register_blueprint(shell_bp)


def startup():
    """Runs once when the server starts."""
    db.init_db()

    # Fetch our own Zammad user ID so the scheduler can tell the persona's
    # articles apart from the operator's.
    try:
        me = zammad.get_current_user()
        scheduler.set_ai_user_id(me["id"])
        print(f"[startup] Zammad connected — api user: {me['email']} (id={me['id']})")
    except Exception as e:
        print(f"[startup] WARNING: Could not reach Zammad: {e}")

    scheduler.start(app)
    print("[startup] Background scheduler started.")


with app.app_context():
    startup()


if __name__ == "__main__":
    from config import FLASK_HOST, FLASK_PORT
    app.run(host=FLASK_HOST, port=FLASK_PORT, debug=True, use_reloader=False)
