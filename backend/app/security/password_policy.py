"""Password and username rules for registration."""

import os
import re

MIN_PASSWORD_LENGTH = int(os.getenv("MIN_PASSWORD_LENGTH", "8"))
MAX_PASSWORD_LENGTH = int(os.getenv("MAX_PASSWORD_LENGTH", "128"))
MIN_USERNAME_LENGTH = int(os.getenv("MIN_USERNAME_LENGTH", "3"))
MAX_USERNAME_LENGTH = int(os.getenv("MAX_USERNAME_LENGTH", "32"))

_USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]+$")


def validate_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError(
            f"Password must be at most {MAX_PASSWORD_LENGTH} characters."
        )
    return password


def validate_username(username: str) -> str:
    if len(username) < MIN_USERNAME_LENGTH:
        raise ValueError(
            f"Username must be at least {MIN_USERNAME_LENGTH} characters."
        )
    if len(username) > MAX_USERNAME_LENGTH:
        raise ValueError(
            f"Username must be at most {MAX_USERNAME_LENGTH} characters."
        )
    if not _USERNAME_PATTERN.match(username):
        raise ValueError(
            "Username may only contain letters, numbers, dots, hyphens, and underscores."
        )
    return username
