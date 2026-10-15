"""
Client for the IT lab (labd in the `lab` container).

The lab replaces the scripted shell for scenarios that have a real machine: it
clones a frozen template, injects the scenario's faults, gives the operator a
real terminal (and the user's desktop for GUI scenarios), measures the machine's
state, and produces an evidence report for grading.

helpdesk-sim keeps owning the ticket side (Zammad, persona, timing, grading call);
the lab owns everything about the machine.
"""
import time
import requests
from config import LAB_URL, LAB_API_KEY, LAB_TRACK, LAB_PUBLIC_PREFIX

_catalog_cache = {"at": 0, "data": None}


class LabUnavailable(Exception):
    pass


def enabled():
    return bool(LAB_URL and LAB_API_KEY)


def _req(method, path, timeout=30, **kw):
    try:
        r = requests.request(method, f"{LAB_URL}{path}", headers={"X-Lab-Key": LAB_API_KEY},
                             timeout=timeout, **kw)
    except requests.RequestException as e:
        raise LabUnavailable(f"lab unreachable: {e}")
    if r.status_code >= 400:
        try:
            msg = r.json().get("error", r.text)
        except ValueError:
            msg = r.text
        raise LabUnavailable(msg) if r.status_code in (409, 503) or r.status_code >= 500 else RuntimeError(msg)
    return r.json()


def catalog(max_age=60):
    """Lab scenarios for this app's track, cached briefly. Empty if the lab is off."""
    if not enabled():
        return {"tracks": {}, "scenarios": []}
    if _catalog_cache["data"] and time.time() - _catalog_cache["at"] < max_age:
        return _catalog_cache["data"]
    try:
        data = _req("GET", f"/api/catalog?track={LAB_TRACK}", timeout=10)
    except Exception:
        return _catalog_cache["data"] or {"tracks": {}, "scenarios": []}
    _catalog_cache.update(at=time.time(), data=data)
    return data


def scenarios():
    """{short_id: scenario} for ready lab scenarios, e.g. 'dns-override'."""
    out = {}
    for s in catalog()["scenarios"]:
        if s.get("ready"):
            out[s["id"].split("/", 1)[1]] = s
    return out


def track():
    return catalog()["tracks"].get(LAB_TRACK, {})


def start_session(short_id, ref):
    return _req("POST", "/api/sessions", json={"scenario": f"{LAB_TRACK}/{short_id}", "ref": ref})


def wait_ready(session_id, timeout=240):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = status(session_id)
        if st["state"] == "ready":
            return st
        if st["state"] != "provisioning":
            raise LabUnavailable(st.get("error") or f"lab session {st['state']}")
        time.sleep(1)
    raise LabUnavailable("lab machine took too long to start")


def status(session_id):
    return _req("GET", f"/api/sessions/{session_id}", timeout=10)


def run_command(session_id, command):
    return _req("POST", f"/api/sessions/{session_id}/exec", json={"command": command}, timeout=45)


def finish(session_id, action):
    return _req("POST", f"/api/sessions/{session_id}/finish", json={"action": action}, timeout=300)


def report(session_id):
    return _req("GET", f"/api/sessions/{session_id}/report", timeout=30)


def destroy(session_id):
    try:
        return _req("DELETE", f"/api/sessions/{session_id}", timeout=60)
    except Exception:
        return None


def console_urls(session_id, token, access):
    """Browser URLs (through the public edge) for the consoles this scenario offers."""
    base = f"{LAB_PUBLIC_PREFIX}/console/{session_id}"
    urls = {}
    if "terminal" in access:
        urls["terminal"] = f"{base}/terminal?t={token}"
    if "gui" in access:
        urls["gui"] = f"{base}/gui?t={token}"
    urls["state"] = f"{base}/state?t={token}"
    return urls
