"""Authentication: session login, password hashing, and CSRF protection.

Sessions are stored in Flask's signed cookie (keyed by SECRET_KEY), so no
extra infrastructure is needed. Passwords are hashed with Werkzeug's PBKDF2.
"""
import functools
import re
import secrets

from flask import abort, flash, g, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import db

USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,30}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LENGTH = 8


# --- password hashing ---------------------------------------------------

def hash_password(password):
    return generate_password_hash(password)


def verify_password(password_hash, password):
    return check_password_hash(password_hash, password)


# --- session management -------------------------------------------------

def login_user(user_id):
    """Log a user in, rotating the session to prevent fixation."""
    session.clear()
    session["user_id"] = user_id
    session.permanent = True


def logout_user():
    session.clear()


def load_logged_in_user():
    """before_request hook: populate g.user from the session (or None)."""
    user_id = session.get("user_id")
    if user_id is None:
        g.user = None
        return
    g.user = db.get_db().execute(
        "SELECT id, username, email, created_at, belt FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if g.user is None:
        # Session points at a deleted user; drop it.
        session.clear()


def login_required(view):
    @functools.wraps(view)
    def wrapped_view(*args, **kwargs):
        if g.user is None:
            flash("Please log in to continue.", "error")
            return redirect(url_for("login", next=request.full_path))
        return view(*args, **kwargs)

    return wrapped_view


# --- CSRF ---------------------------------------------------------------

def get_csrf_token():
    """Return the session's CSRF token, creating one on first use."""
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def check_csrf():
    """before_request hook: reject POSTs without a valid CSRF token.

    /api/ endpoints are exempt: they authenticate with a bearer token, which
    browsers never attach automatically, so they aren't CSRF-able.
    """
    if request.method == "POST" and not request.path.startswith("/api/"):
        expected = session.get("csrf_token")
        provided = request.form.get("csrf_token", "")
        if not expected or not secrets.compare_digest(expected, provided):
            abort(400, description="Invalid or missing CSRF token.")


# --- validation ---------------------------------------------------------

def validate_registration(username, email, password):
    """Return an error message, or None if the fields are valid."""
    if not USERNAME_RE.match(username):
        return "Username must be 3-30 characters: letters, numbers, or underscore."
    if not EMAIL_RE.match(email):
        return "Please enter a valid email address."
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    return None
