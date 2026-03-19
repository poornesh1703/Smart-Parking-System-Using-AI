from functools import wraps
from flask import abort
from flask_login import current_user

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not getattr(current_user, "is_authenticated", False) or current_user.role != "admin":
            return abort(403)
        return fn(*args, **kwargs)
    return wrapper
