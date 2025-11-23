from flask import session, redirect
from functools import wraps

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("logged"):
            return redirect("/login")
        return f(*args, **kwargs)
    return wrapper
